import pytest
from httpx import AsyncClient

from app.api.routes import health as health_module


async def test_live_always_ok(client: AsyncClient) -> None:
    r = await client.get("/api/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_when_dependencies_up(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "database": "ok", "storage": "ok"}


async def test_503_when_storage_down(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise ConnectionError("storage unreachable")

    monkeypatch.setattr(health_module, "check_buckets", broken)
    r = await client.get("/api/health")
    assert r.status_code == 503
    assert r.json() == {"status": "fail", "database": "ok", "storage": "fail"}


async def test_503_when_database_down(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_sessionmaker():
        raise ConnectionRefusedError("database unreachable")

    monkeypatch.setattr(health_module, "get_sessionmaker", broken_sessionmaker)
    r = await client.get("/api/health")
    assert r.status_code == 503
    assert r.json()["database"] == "fail"


async def test_swagger_available(client: AsyncClient) -> None:
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/health" in r.json()["paths"]
