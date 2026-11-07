"""Регистрация, вход, выход, проверка сессии (ADR 0005)."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.errors import Conflict, Forbidden, Unauthorized
from app.models import User
from app.repositories import sessions as sessions_repo
from app.repositories import users as users_repo
from app.schemas.auth import RegisterIn

INVALID_CREDENTIALS = "Неверный логин или пароль"
ACCOUNT_BANNED = "Аккаунт заблокирован"
EMAIL_TAKEN = "Пользователь с таким email уже зарегистрирован"
USERNAME_TAKEN = "Это имя пользователя уже занято"
MAX_PASSWORD_LEN = 128

# Уникальные ограничения → сообщение. Любое другое нарушение целостности — не «занято»,
# а ошибка, которую нельзя маскировать.
_CONFLICT_BY_CONSTRAINT = {
    "uq_users_email": EMAIL_TAKEN,
    "uq_users_username": USERNAME_TAKEN,
    "uq_channels_handle": USERNAME_TAKEN,
}

# argon2id по RFC 9106 занимает 64 МиБ памяти на вызов. Ограничиваем параллельные вызовы
# в процессе: поток одновременных входов не должен исчерпать память (DoS).
_ARGON2_SLOTS = asyncio.Semaphore(2)


async def _argon2(fn, *args):
    async with _ARGON2_SLOTS:
        return await asyncio.to_thread(fn, *args)


def _constraint_name(exc: IntegrityError) -> str | None:
    # exc.orig — адаптер DBAPI; исходное исключение asyncpg (с constraint_name) — его __cause__.
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


@dataclass(frozen=True)
class ClientInfo:
    ip: str | None
    user_agent: str | None


@dataclass(frozen=True)
class AuthResult:
    user: User
    token: str


async def _open_session(db: AsyncSession, user: User, client: ClientInfo) -> str:
    token = security.new_token()
    await sessions_repo.create(
        db,
        user_id=user.id,
        token_hash=security.hash_token(token),
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().session_ttl_days),
        ip=client.ip,
        user_agent=client.user_agent,
    )
    return token


async def _revoke_previous(db: AsyncSession, previous_token: str | None) -> None:
    """Повторный вход/регистрация в том же браузере: старая сессия удаляется,
    а не остаётся «сиротой» в БД до истечения."""
    if previous_token:
        await sessions_repo.delete_by_hash(db, security.hash_token(previous_token))


async def register(
    db: AsyncSession, data: RegisterIn, client: ClientInfo, previous_token: str | None = None
) -> AuthResult:
    # Дружелюбные сообщения для частого случая; гонку двух регистраций ловит
    # уникальный индекс (citext) ниже.
    if await users_repo.email_taken(db, data.email):
        raise Conflict(EMAIL_TAKEN)
    if await users_repo.username_taken(db, data.username):
        raise Conflict(USERNAME_TAKEN)

    # argon2 — это ~десятки мс CPU: не блокируем event loop.
    password_hash = await _argon2(security.hash_password, data.password)
    try:
        user = await users_repo.create_with_channel(
            db, email=data.email, username=data.username, password_hash=password_hash
        )
        await _revoke_previous(db, previous_token)
        token = await _open_session(db, user, client)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        message = _CONFLICT_BY_CONSTRAINT.get(_constraint_name(exc) or "")
        if message is None:
            raise
        raise Conflict(message) from exc
    return AuthResult(user=user, token=token)


async def login(
    db: AsyncSession,
    login: str,
    password: str,
    client: ClientInfo,
    previous_token: str | None = None,
) -> AuthResult:
    if len(password) > MAX_PASSWORD_LEN or len(login) > 254:
        raise Unauthorized(INVALID_CREDENTIALS)
    user = await users_repo.get_by_login(db, login.strip())
    if user is None:
        # Одинаковые время и ответ для «нет такого» и «неверный пароль».
        await _argon2(security.burn_password_check, password)
        await db.rollback()
        raise Unauthorized(INVALID_CREDENTIALS)

    ok = await _argon2(security.verify_password, user.password_hash, password)
    if not ok:
        await db.rollback()
        raise Unauthorized(INVALID_CREDENTIALS)
    # Сообщаем о бане только после верного пароля — без раскрытия третьим лицам.
    if user.is_banned:
        await db.rollback()
        raise Forbidden(ACCOUNT_BANNED)

    if security.needs_rehash(user.password_hash):
        user.password_hash = await _argon2(security.hash_password, password)
    await _revoke_previous(db, previous_token)
    token = await _open_session(db, user, client)
    await db.commit()
    return AuthResult(user=user, token=token)


async def logout(db: AsyncSession, token: str) -> None:
    await sessions_repo.delete_by_hash(db, security.hash_token(token))
    await db.commit()


async def authenticate(db: AsyncSession, token: str) -> User | None:
    """Пользователь по токену сессии; None — токен неизвестен, истёк или пользователь забанен.

    Бан проверяется на каждом запросе — действует мгновенно, даже без удаления сессий.
    Транзакция всегда завершается (commit/rollback): вызывающий код получает сессию БД
    в одинаковом состоянии независимо от результата.
    """
    found = await sessions_repo.get_active_user(db, security.hash_token(token))
    if found is None or found[1].is_banned:
        await db.rollback()
        return None
    session_id, user = found
    await sessions_repo.touch(db, session_id)
    await db.commit()
    return user


async def get_me(db: AsyncSession, user_id) -> User:
    user = await users_repo.get_with_channel(db, user_id)
    # Только чтение — закрываем транзакцию commit-ом: rollback пометил бы загруженные
    # объекты устаревшими (даже при expire_on_commit=False), и чтение атрибутов дало бы
    # MissingGreenlet.
    await db.commit()
    if user is None:  # удалён между запросами
        raise Unauthorized("Требуется вход")
    return user
