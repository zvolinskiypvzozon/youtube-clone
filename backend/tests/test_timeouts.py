"""statement_timeout API (шаг 2 дня 6): 5 с на запрос, превышение → 503 + Retry-After;
бан и разбан — свой предел 30 с. Worker и alembic этого предела не получают — проверяется
вручную на стеке (у них свои движки и окружение, см. docs/security.md)."""

import uuid

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import DB_BUSY
from app.models import UserRole
from app.repositories import counters as counters_repo
from app.services import bans as bans_service
from tests.helpers import api_user


async def test_api_engine_statement_timeout_is_5s(db: AsyncSession) -> None:
    assert get_settings().db_statement_timeout_ms == 5000
    assert await db.scalar(text("SHOW statement_timeout")) == "5s"
    assert await db.scalar(text("SHOW lock_timeout")) == "5s"


async def test_statement_timeout_is_503() -> None:
    """Настоящая ошибка Postgres 57014 (query_canceled) через обработчик приложения. Предел
    понижен в самой транзакции, чтобы тест не ждал 5 с."""
    from app.main import create_app

    app = create_app()

    @app.get("/api/__test_slow")
    async def slow(db: AsyncSession = Depends(get_session)) -> None:
        await db.execute(text("SET LOCAL statement_timeout = 100"))
        await db.execute(text("SELECT pg_sleep(1)"))

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/__test_slow")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json() == {"detail": DB_BUSY}


async def test_ban_transaction_has_30s_statement_timeout(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пересчёт при бане идёт с пределом 30 с, а не общими 5 с API."""
    seen: list[str] = []
    real = counters_repo.videos_touched_by

    async def spy(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
        seen.append(await session.scalar(text("SHOW statement_timeout")))
        return await real(session, user_id)

    monkeypatch.setattr(counters_repo, "videos_touched_by", spy)
    mod = await api_user(client, db, UserRole.moderator)
    target = await api_user(client, db)
    r = await client.put(
        f"/api/admin/users/{target['id']}/ban", json={"reason": "спам"}, headers=mod["auth"]
    )
    assert r.status_code == 200, r.text
    r = await client.delete(f"/api/admin/users/{target['id']}/ban", headers=mod["auth"])
    assert r.status_code == 200, r.text
    assert seen == ["30s", "30s"]
    assert bans_service.BAN_STATEMENT_TIMEOUT_MS == 30_000


async def test_ban_statement_timeout_does_not_leak_to_connection(
    client: AsyncClient, db: AsyncSession
) -> None:
    """SET LOCAL, а не SET: после commit бана то же соединение снова с пределом 5 с
    (иначе соединение вернулось бы в пул с 30 с)."""
    from app.models import User

    mod = await api_user(client, db, UserRole.moderator)
    target = await api_user(client, db)
    me = await db.get(User, uuid.UUID(mod["id"]))
    await bans_service.ban(db, me, uuid.UUID(target["id"]), "спам")  # commit внутри
    assert await db.scalar(text("SHOW statement_timeout")) == "5s"
