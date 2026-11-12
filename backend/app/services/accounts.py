"""Удаление пользователя (день 9): сам — с паролем (DELETE /api/me), админ — user и moderator
(DELETE /api/admin/users/{id}); последнего администратора удалить нельзя никак (409).

Две фазы:
  A. Видео пользователя удаляются по одному через services/deletion — порядок «задачи →
     видео», повторы при гонке с complete_upload, объекты хранилища после commit (ADR 0006
     доп. 3: любой путь, удаляющий видео, — через services/deletion).
  B. Одна транзакция, порядок блокировок как у бана (ADR 0006 доп. 8): блокировка смен ролей
     → строка пользователя → его канал (новые загрузки ждут) → если видео появились за это
     время — rollback и снова A → видео и каналы, которых касались его реакции, комментарии и
     подписки → ник в retired_usernames → DELETE пользователя (каскад) → пересчёт счётчиков
     тех видео и каналов (recount, как при бане) → commit.

Остаётся: жалобы, поданные им (reporter_id → NULL), жалобы на его видео (снимок названия,
handle и channel_id), журнал модерации (actor_id → NULL). Ник занят навсегда; email не
хранится — повторная регистрация с ним под другим ником разрешена.
"""

import contextlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.errors import Busy, Conflict, Forbidden, NotFound
from app.models import ModerationAction, User, UserRole
from app.repositories import counters as counters_repo
from app.repositories import moderation as moderation_repo
from app.repositories import users as users_repo
from app.repositories import videos as videos_repo
from app.services import deletion, policies
from app.services.auth import _argon2

USER_NOT_FOUND = "Пользователь не найден"
CONFIRMATION_FAILED = "Неверный пароль"
CANNOT_DELETE = "Недостаточно прав: удалить можно только пользователя или модератора"
LAST_ADMIN = "Нельзя удалить последнего администратора"
USER_BUSY = "Аккаунт сейчас изменяется. Повторите попытку через несколько секунд"
# Как у бана и удаления видео: удаление затрагивает много строк (журнал, счётчики).
DELETE_USER_STATEMENT_TIMEOUT_MS = 30_000
DELETE_USER_ATTEMPTS = 3


async def delete_self(db: AsyncSession, user: User, password: str) -> None:
    """Пароль проверяется до всего остального (argon2 — в пуле потоков). Неверный — 403
    (перебор ограничивает зона limit_req auth в nginx, как у входа)."""
    user_id, password_hash = user.id, user.password_hash
    if not await _argon2(security.verify_password, password_hash, password):
        raise Forbidden(CONFIRMATION_FAILED)
    await _delete(db, actor_id=user_id, user_id=user_id, by_admin=False)


async def delete_by_admin(db: AsyncSession, actor: User, user_id: uuid.UUID) -> None:
    await _delete(db, actor_id=actor.id, user_id=user_id, by_admin=True)


async def _delete(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID, by_admin: bool
) -> None:
    # Права и «последний админ» — до фазы A: она необратимо удаляет видео и файлы, и отказ
    # после неё оставлял бы цель без контента (аудит дня 9, High). Фаза B перепроверяет то же
    # под теми же блокировками — на случай гонки (смена роли между проверкой и удалением).
    await _check(db, actor_id=actor_id, user_id=user_id, by_admin=by_admin)
    for _ in range(DELETE_USER_ATTEMPTS):
        await _delete_videos(db, user_id)
        if await _delete_account(db, actor_id=actor_id, user_id=user_id, by_admin=by_admin):
            return
    raise Busy(USER_BUSY)


async def _locked_target(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID, by_admin: bool
) -> User:
    """Под блокировкой смен ролей: строка цели и проверки (права админа, последний админ).
    Вызывать внутри транзакции; при ошибке вызывающий делает rollback."""
    await users_repo.lock_role_changes(db)
    actor_role = await users_repo.get_active_role(db, actor_id)
    target = await users_repo.lock_for_ban(db, user_id)
    if target is None:
        raise NotFound(USER_NOT_FOUND)
    if by_admin and not policies.can_delete_user(actor_id, actor_role, target.id, target.role):
        raise Forbidden(CANNOT_DELETE)
    # Два удаления (или удаление и понижение) не оставят систему без администратора.
    if target.role == UserRole.admin and await users_repo.count_admins(db) <= 1:
        raise Conflict(LAST_ADMIN)
    return target


async def _check(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID, by_admin: bool
) -> None:
    """Предпроверка без изменений: отказ (403/404/409) — до удаления видео."""
    try:
        await _locked_target(db, actor_id=actor_id, user_id=user_id, by_admin=by_admin)
    finally:
        await db.rollback()


async def _delete_videos(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Фаза A: все видео канала — через services/deletion (каждое — своей транзакцией)."""
    user = await users_repo.get_with_channel(db, user_id)
    if user is None or user.channel is None:
        await db.rollback()
        return
    video_ids = await videos_repo.ids_of_channel(db, user.channel.id)
    await db.rollback()  # закрыть читающую транзакцию перед удалениями
    for video_id in video_ids:
        with contextlib.suppress(NotFound):  # уже удалено параллельно
            await deletion.delete_video(db, video_id)


async def _delete_account(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID, by_admin: bool
) -> bool:
    """Фаза B. False — за время фазы A появились новые видео: повторить A."""
    try:
        await users_repo.set_local_statement_timeout(db, DELETE_USER_STATEMENT_TIMEOUT_MS)
        target = await _locked_target(db, actor_id=actor_id, user_id=user_id, by_admin=by_admin)

        channel = await users_repo.lock_channel_of(db, user_id)
        if channel is not None and await videos_repo.ids_of_channel(db, channel.id):
            await db.rollback()
            return False

        video_ids = await counters_repo.videos_touched_by(db, user_id)
        channel_ids = await counters_repo.channels_subscribed_by(db, user_id)
        username, role = target.username, target.role
        await users_repo.retire_username(db, username)
        if by_admin:
            await moderation_repo.log(
                db,
                actor_id=actor_id,
                action=ModerationAction.user_delete,
                target_id=user_id,
                details={"username": username, "role": role.value},
            )
        await users_repo.delete_user(db, user_id)
        await counters_repo.recount_videos(db, video_ids)
        await counters_repo.recount_channels(db, channel_ids)
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        raise
