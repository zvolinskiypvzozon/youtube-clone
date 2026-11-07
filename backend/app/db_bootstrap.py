"""Подготовка ролей БД (выполняется сервисом migrate перед `alembic upgrade head`).

Postgres-образ делает POSTGRES_USER суперпользователем. Под ним выполняются только
миграции (владелец схемы). api и worker подключаются под отдельной ролью без SUPERUSER,
CREATEDB, CREATEROLE и без права создавать объекты: только DML на таблицах.
Тогда SQL-инъекция не даёт `COPY ... TO PROGRAM` и изменения схемы.

Идемпотентно: роль создаётся или обновляется (пароль), гранты выдаются повторно.

Переменные окружения:
  DATABASE_URL       — URL владельца (postgresql+asyncpg://owner:pass@host/db)
  APP_DB_USER        — имя роли приложения
  APP_DB_PASSWORD    — пароль роли приложения
"""

import asyncio
import os
import re

import asyncpg

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SECRET = re.compile(r"^[A-Za-z0-9]{16,128}$")


def _plain_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def ensure_app_role(owner_url: str, app_user: str, app_password: str) -> None:
    # DDL не поддерживает bind-параметры, поэтому значения строго валидируются.
    if not _IDENT.match(app_user):
        raise ValueError("APP_DB_USER: недопустимое имя роли")
    if not _SECRET.match(app_password):
        raise ValueError("APP_DB_PASSWORD: ожидаются 16-128 символов [A-Za-z0-9]")

    conn = await asyncpg.connect(_plain_dsn(owner_url))
    try:
        owner = await conn.fetchval("SELECT current_user")
        db = await conn.fetchval("SELECT current_database()")
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", app_user)
        verb = "ALTER" if exists else "CREATE"
        await conn.execute(
            f"{verb} ROLE {app_user} LOGIN PASSWORD '{app_password}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
        await conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {app_user}')
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {app_user}")
        await conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {app_user}")
        await conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_user}"
        )
        await conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_user}")
        # Для таблиц, которые миграции создадут позже.
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_user}"
        )
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {app_user}"
        )
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(
        ensure_app_role(
            os.environ["DATABASE_URL"],
            os.environ["APP_DB_USER"],
            os.environ["APP_DB_PASSWORD"],
        )
    )
    print("db bootstrap: app role ready")


if __name__ == "__main__":
    main()
