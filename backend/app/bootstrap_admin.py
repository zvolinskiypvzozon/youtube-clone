"""Создание первого администратора (сервис migrate, после `alembic upgrade head`).

Правила (строго, чтобы нельзя было «подсунуть» чужой аккаунт):
  - ни username, ни email не заняты → администратор создаётся (с каналом);
  - оба указывают на одного и того же пользователя с ролью admin → ничего не меняется
    (в том числе пароль: повторный деплой не сбрасывает сменённый пароль);
  - любой другой случай (имя занято обычным пользователем, email у другого аккаунта и т.п.) →
    ошибка и код выхода 1. migrate падает, деплой останавливается с понятным сообщением,
    а не выдаёт оператору пароль, который ни к чему не подходит.

Переменные окружения:
  DATABASE_URL     — URL БД (в migrate — владелец схемы)
  ADMIN_USERNAME   — имя администратора
  ADMIN_EMAIL      — email администратора
  ADMIN_PASSWORD   — пароль (генерируется scripts/gen-env.sh, хранится в .env с правами 600)
"""

import asyncio
import os
import re
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import hash_password
from app.models import Channel, User, UserRole

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
CREATED_MARKER = "bootstrap admin: создан"


class BootstrapError(Exception):
    pass


async def ensure_admin(database_url: str, username: str, email: str, password: str) -> bool:
    """True — администратор создан, False — уже существовал. BootstrapError — конфликт."""
    if not USERNAME_RE.match(username):
        raise BootstrapError("ADMIN_USERNAME: 3–30 символов, латиница, цифры, _")
    if "@" not in email[1:]:
        raise BootstrapError("ADMIN_EMAIL: неверный формат")
    if len(password) < 8:
        raise BootstrapError("ADMIN_PASSWORD: минимум 8 символов")

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            by_name = await db.scalar(select(User).where(User.username == username))
            by_email = await db.scalar(select(User).where(User.email == email))

            if by_name is None and by_email is None:
                handle_taken = await db.scalar(select(Channel.id).where(Channel.handle == username))
                if handle_taken is not None:
                    raise BootstrapError(f"имя канала «{username}» уже занято")
                user = User(
                    email=email,
                    username=username,
                    password_hash=hash_password(password),
                    role=UserRole.admin,
                )
                db.add(user)
                await db.flush()
                db.add(Channel(owner_id=user.id, handle=username, title=username))
                await db.commit()
                return True

            same_account = (
                by_name is not None and by_email is not None and by_name.id == by_email.id
            )
            if same_account and by_name.role == UserRole.admin:
                return False
            raise BootstrapError(
                f"ADMIN_USERNAME «{username}» или ADMIN_EMAIL «{email}» уже занят(ы) другой "
                "учётной записью (не администратором или не одной и той же). "
                "Задайте в .env свободные ADMIN_USERNAME и ADMIN_EMAIL и повторите деплой."
            )
    finally:
        await engine.dispose()


def main() -> None:
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not (username and email and password):
        print("bootstrap admin: ADMIN_* не заданы — пропускаю")
        return
    try:
        created = asyncio.run(ensure_admin(os.environ["DATABASE_URL"], username, email, password))
    except BootstrapError as exc:
        print(f"bootstrap admin: ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
    # Маркер «создан» читает deploy.sh, чтобы показать пароль только при создании.
    print(
        f"{CREATED_MARKER} ({username})"
        if created
        else f"bootstrap admin: уже существует ({username})"
    )


if __name__ == "__main__":
    main()
