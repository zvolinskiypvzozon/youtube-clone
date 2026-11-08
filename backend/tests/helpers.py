"""Общие помощники тестов API."""

import asyncio
import time
import uuid

from httpx import AsyncClient
from sqlalchemy import text

from app.core.db import get_sessionmaker

PASSWORD = "test-password-42"


async def new_user(client: AsyncClient) -> dict:
    """Регистрирует пользователя и возвращает id и Bearer-заголовок.

    Bearer, а не cookie: тестам загрузки CSRF не нужен (он проверяется отдельно).
    """
    s = uuid.uuid4().hex[:8]
    creds = {"email": f"u_{s}@example.com", "username": f"u_{s}", "password": PASSWORD}
    r = await client.post("/api/auth/register", json=creds)
    assert r.status_code == 201, r.text
    client.cookies.clear()
    tok = await client.post(
        "/api/auth/token", data={"username": creds["username"], "password": PASSWORD}
    )
    return {
        "id": r.json()["id"],
        "username": creds["username"],
        "auth": {"Authorization": f"Bearer {tok.json()['access_token']}"},
    }


LOCK_WAIT_SECONDS = 5.0


async def backend_pid(session) -> int:
    """pid сеанса Postgres этой сессии (держателя блокировки в тестах гонок)."""
    return await session.scalar(text("SELECT pg_backend_pid()"))


async def wait_for_lock_waiters(blocker_pid: int, count: int = 1) -> None:
    """Ждёт, пока `count` сеансов встанут в ожидание блокировки, которую держит именно
    `blocker_pid` (детерминированные тесты гонок: «второй ждёт первого» проверяется по факту,
    а не паузой; посторонние ожидающие в той же БД не учитываются)."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    async with get_sessionmaker()() as probe:
        while True:
            waiting = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :blocker = ANY(pg_blocking_pids(pid))"
                ),
                {"blocker": blocker_pid},
            )
            await probe.rollback()
            if waiting >= count:
                return
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"ожидающих блокировку {blocker_pid}: {waiting}, нужно {count}"
                )
            await asyncio.sleep(0.02)
