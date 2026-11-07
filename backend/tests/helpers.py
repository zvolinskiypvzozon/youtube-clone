"""Общие помощники тестов API."""

import uuid

from httpx import AsyncClient

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
