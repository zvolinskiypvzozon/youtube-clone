"""Тесты идут против реальных Postgres и хранилища (сервис `tests` в docker-compose).

Изоляция от боевых данных: отдельная БД `<db>_test` и отдельные бакеты `yt-test-*`
(создаёт storage-init). Тесты можно запускать и на развёрнутом сервере.

На каждый запуск:
  - БД `<db>_test` пересоздаётся с нуля (нет «хвостов» от прошлых прогонов и правок миграций);
  - миграции проходят цикл upgrade → downgrade → upgrade (проверка обратимости в CI);
  - после каждого теста таблицы очищаются (TRUNCATE под владельцем: у роли приложения
    этого права нет намеренно).

Параллельные прогоны (например, CI и ручной на сервере) сериализуются advisory-блокировкой:
второй ждёт первого, а не обрывает его соединения через DROP DATABASE ... WITH (FORCE).
pytest-xdist не используется (общая БД на процесс).
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.db_bootstrap import ensure_app_role
from app.models import Base

TEST_DB_SUFFIX = "_test"
TEST_RUN_LOCK_ID = 7_000_001  # произвольная константа для pg_advisory_lock


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _owner_test_url() -> str:
    owner_url = os.environ["OWNER_DATABASE_URL"]
    test_db = get_settings().database_url.rsplit("/", 1)[1]
    assert test_db.endswith(TEST_DB_SUFFIX), "тесты обязаны работать с отдельной БД *_test"
    return owner_url.rsplit("/", 1)[0] + "/" + test_db


async def _recreate_test_database() -> str:
    owner_url = os.environ["OWNER_DATABASE_URL"]
    owner_test_url = _owner_test_url()
    test_db = owner_test_url.rsplit("/", 1)[1]

    admin = await asyncpg.connect(_plain(owner_url))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{test_db}"')
    finally:
        await admin.close()

    await ensure_app_role(owner_test_url, os.environ["APP_DB_USER"], os.environ["APP_DB_PASSWORD"])
    return owner_test_url


def _migrate_roundtrip() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
async def test_database() -> AsyncIterator[None]:
    # Блокировка держится на соединении к рабочей БД всё время прогона.
    lock_conn = await asyncpg.connect(_plain(os.environ["OWNER_DATABASE_URL"]))
    await lock_conn.execute("SELECT pg_advisory_lock($1)", TEST_RUN_LOCK_ID)
    try:
        os.environ["ALEMBIC_DATABASE_URL"] = await _recreate_test_database()
        # env.py вызывает asyncio.run — выполняем в отдельном потоке со своим циклом.
        await asyncio.to_thread(_migrate_roundtrip)
        yield
    finally:
        await lock_conn.close()  # закрытие соединения снимает блокировку


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncIterator[None]:
    yield
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    conn = await asyncpg.connect(_plain(_owner_test_url()))
    try:
        await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Сессия под ролью приложения (yt_app) — те же права, что у api/worker."""
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def relative_urls(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Режим продакшена: без S3_PUBLIC_ENDPOINT ссылки относительные, подпись на
    SIGNING_HOST (ADR 0004, дополнение 1). Кэш клиента подписи — заново до и после."""
    from app.core import storage

    monkeypatch.setattr(get_settings(), "s3_public_endpoint", None)
    storage._presign_client.cache_clear()
    yield
    storage._presign_client.cache_clear()
