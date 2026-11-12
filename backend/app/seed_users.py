"""Демо-учётки для проверки (scripts/seed.sh → scripts/seed.py → этот модуль в контейнере api).

Набор учёток и ролей зашит здесь; пароли приходят через stdin (JSON {ник: пароль}) — не через
переменные окружения (видны в `docker inspect`) и не в коде (решение архитектора дня 9: пароли
генерируются при запуске сида и хранятся в .deploy/demo-credentials с правами 600).

Правила (как у bootstrap_admin — нельзя «подсунуть» чужой аккаунт):
  - ни ник, ни email не заняты → учётка создаётся с каналом;
  - ник и email принадлежат одной демо-учётке → пароль и роль приводятся к переданным, бан
    снимается с пересчётом счётчиков тем же кодом, что разбан (иначе забаненная при проверке
    демо-учётка ломала бы каждый следующий деплой — ревью дня 9); файл паролей — источник
    истины; повторный сид не создаёт дублей;
  - любой другой случай (ник занят обычным пользователем, email у другого аккаунта, handle
    канала занят) → ошибка и код выхода 1, сид останавливается.
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import hash_password
from app.models import Channel, User, UserRole
from app.services import bans as bans_service

MIN_PASSWORD = 12


@dataclass(frozen=True)
class DemoUser:
    username: str
    role: UserRole
    title: str

    @property
    def email(self) -> str:
        # .invalid — зарезервированный домен (RFC 2606): письма не уйдут никуда.
        return f"{self.username}@demo.invalid"


DEMO_USERS = (
    DemoUser("demo_admin", UserRole.admin, "Демо-админ"),
    DemoUser("demo_moderator", UserRole.moderator, "Демо-модератор"),
    DemoUser("demo_user", UserRole.user, "Демо-зритель"),
    DemoUser("demo_author", UserRole.user, "Демо-автор"),
)


class SeedError(Exception):
    pass


async def ensure_demo_users(database_url: str, passwords: dict[str, str]) -> dict[str, str]:
    """{ник: "создан" | "обновлён"}. SeedError — неполный ввод или конфликт с чужим аккаунтом."""
    missing = [u.username for u in DEMO_USERS if u.username not in passwords]
    if missing:
        raise SeedError(f"нет паролей для: {', '.join(missing)}")
    short = [u.username for u in DEMO_USERS if len(passwords[u.username]) < MIN_PASSWORD]
    if short:
        raise SeedError(f"пароль короче {MIN_PASSWORD} символов: {', '.join(short)}")

    result: dict[str, str] = {}
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            for demo in DEMO_USERS:
                by_name = await db.scalar(select(User).where(User.username == demo.username))
                by_email = await db.scalar(select(User).where(User.email == demo.email))
                password_hash = hash_password(passwords[demo.username])

                if by_name is None and by_email is None:
                    handle_taken = await db.scalar(
                        select(Channel.id).where(Channel.handle == demo.username)
                    )
                    if handle_taken is not None:
                        raise SeedError(f"имя канала «{demo.username}» уже занято")
                    user = User(
                        email=demo.email,
                        username=demo.username,
                        password_hash=password_hash,
                        role=demo.role,
                    )
                    db.add(user)
                    await db.flush()
                    db.add(Channel(owner_id=user.id, handle=demo.username, title=demo.title))
                    result[demo.username] = "создан"
                    continue

                if by_name is not None and by_email is not None and by_name.id == by_email.id:
                    by_name.password_hash = password_hash
                    by_name.role = demo.role
                    if by_name.is_banned:
                        by_name.is_banned = False
                        by_name.banned_reason = None
                        by_name.banned_by_role = None
                        await db.flush()
                        await bans_service.recount_for_user(db, by_name.id)
                        result[demo.username] = "обновлён, бан снят"
                    else:
                        result[demo.username] = "обновлён"
                    continue

                raise SeedError(
                    f"ник «{demo.username}» или email «{demo.email}» занят другой учётной записью"
                )
            # Одной транзакцией: при конфликте на любой учётке не остаётся половины набора.
            await db.commit()
    finally:
        await engine.dispose()
    return result


def main() -> None:
    try:
        passwords = json.loads(sys.stdin.read())
        if not isinstance(passwords, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in passwords.items()
        ):
            raise SeedError("на stdin ожидается JSON-объект {ник: пароль}")
        result = asyncio.run(ensure_demo_users(os.environ["DATABASE_URL"], passwords))
    except (SeedError, json.JSONDecodeError) as exc:
        print(f"seed users: ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
    for username, action in result.items():
        print(f"seed users: {username} — {action}")


if __name__ == "__main__":
    main()
