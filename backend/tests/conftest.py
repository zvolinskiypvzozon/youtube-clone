"""Тесты идут против реальных Postgres и хранилища (сервис `tests` в docker-compose).

Изоляция от боевых данных: отдельная БД `<db>_test` (создаётся и мигрируется здесь)
и отдельные бакеты `yt-test-*` (создаёт storage-init). Тесты можно запускать
и на развёрнутом сервере — боевые данные не затрагиваются.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db_bootstrap import ensure_app_role

TEST_DB_SUFFIX = "_test"


async def _prepare_test_database() -> None:
    owner_url = os.environ["OWNER_DATABASE_URL"]
    app_url = get_settings().database_url
    test_db = app_url.rsplit("/", 1)[1]
    assert test_db.endswith(TEST_DB_SUFFIX), "тесты обязаны работать с отдельной БД *_test"

    admin = await asyncpg.connect(owner_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", test_db)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{test_db}"')
    finally:
        await admin.close()

    owner_test_url = owner_url.rsplit("/", 1)[0] + "/" + test_db
    await ensure_app_role(owner_test_url, os.environ["APP_DB_USER"], os.environ["APP_DB_PASSWORD"])
    os.environ["ALEMBIC_DATABASE_URL"] = owner_test_url


@pytest.fixture(scope="session", autouse=True)
async def test_database() -> None:
    await _prepare_test_database()
    cfg = Config("alembic.ini")
    # env.py вызывает asyncio.run — выполняем в отдельном потоке со своим циклом.
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
