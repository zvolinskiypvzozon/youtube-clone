"""Регистрация, вход, выход, сессии, CSRF (ADR 0005)."""

import hashlib
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, User, UserRole, UserSession
from tests.helpers import new_user

PASSWORD = "correct-horse-42"

# CHECK ck_users_ban_consistent: забанен ⇔ есть причина и роль забанившего.
BANNED = {"is_banned": True, "banned_reason": "спам", "banned_by_role": UserRole.admin}


def _creds(**overrides: str) -> dict[str, str]:
    s = uuid.uuid4().hex[:8]
    data = {"email": f"u_{s}@example.com", "username": f"u_{s}", "password": PASSWORD}
    data.update(overrides)
    return data


async def _register(client: AsyncClient, **overrides: str) -> dict:
    creds = _creds(**overrides)
    r = await client.post("/api/auth/register", json=creds)
    assert r.status_code == 201, r.text
    return {**creds, "me": r.json()}


def _csrf(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


# --- регистрация -----------------------------------------------------------------------


async def test_register_creates_user_channel_and_logs_in(client: AsyncClient) -> None:
    creds = _creds()
    r = await client.post("/api/auth/register", json=creds)
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == creds["username"]
    assert body["role"] == "user"
    assert body["channel"]["handle"] == creds["username"]
    assert "password" not in r.text and "password_hash" not in r.text

    set_cookie = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith("session="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    csrf_cookie = next(c for c in set_cookie if c.startswith("csrf="))
    assert "HttpOnly" not in csrf_cookie  # SPA читает его и повторяет в заголовке

    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["id"] == body["id"]


async def test_register_duplicate_email_case_insensitive(client: AsyncClient) -> None:
    first = await _register(client)
    client.cookies.clear()
    r = await client.post("/api/auth/register", json=_creds(email=first["email"].upper()))
    assert r.status_code == 409
    assert "email" in r.json()["detail"]


async def test_register_duplicate_username_case_insensitive(client: AsyncClient) -> None:
    first = await _register(client)
    client.cookies.clear()
    r = await client.post("/api/auth/register", json=_creds(username=first["username"].upper()))
    assert r.status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("email", "not-an-email"),
        ("username", "ab"),
        ("username", "bad name"),
        ("username", "кириллица"),
        ("password", "short"),
        ("password", "x" * 129),
    ],
)
async def test_register_validation(client: AsyncClient, field: str, value: str) -> None:
    r = await client.post("/api/auth/register", json=_creds(**{field: value}))
    assert r.status_code == 422


async def test_password_and_token_never_stored_in_plain(
    client: AsyncClient, db: AsyncSession
) -> None:
    creds = await _register(client)
    user = (await db.execute(select(User).where(User.username == creds["username"]))).scalar_one()
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash

    token = client.cookies["session"]
    stored = (
        await db.execute(select(UserSession.token_hash).where(UserSession.user_id == user.id))
    ).scalar_one()
    assert stored == hashlib.sha256(token.encode()).digest()
    assert token.encode() != stored


# --- вход ------------------------------------------------------------------------------


@pytest.mark.parametrize("by", ["email", "username"])
async def test_login_by_email_or_username(client: AsyncClient, by: str) -> None:
    creds = await _register(client)
    client.cookies.clear()
    login = creds[by].upper()  # без учёта регистра
    r = await client.post("/api/auth/login", json={"login": login, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["username"] == creds["username"]
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_login_does_not_reveal_whether_user_exists(client: AsyncClient) -> None:
    creds = await _register(client)
    client.cookies.clear()
    wrong_password = await client.post(
        "/api/auth/login", json={"login": creds["email"], "password": "wrong-password"}
    )
    no_such_user = await client.post(
        "/api/auth/login", json={"login": "nobody@example.com", "password": "wrong-password"}
    )
    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json() == no_such_user.json()
    assert "session" not in client.cookies


async def test_banned_user_cannot_login(client: AsyncClient, db: AsyncSession) -> None:
    creds = await _register(client)
    client.cookies.clear()
    await db.execute(update(User).where(User.username == creds["username"]).values(BANNED))
    await db.commit()
    r = await client.post("/api/auth/login", json={"login": creds["email"], "password": PASSWORD})
    assert r.status_code == 403
    assert "session" not in client.cookies


async def test_banned_user_existing_session_stops_working(
    client: AsyncClient, db: AsyncSession
) -> None:
    creds = await _register(client)
    await db.execute(update(User).where(User.username == creds["username"]).values(BANNED))
    await db.commit()
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_banned_user_bearer_token_stops_working(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Политика доступа к видео считает забаненного анонимом — это держится на том, что
    authenticate отказывает ему по любому способу входа, в том числе по Bearer."""
    user = await new_user(client)
    assert (await client.get("/api/auth/me", headers=user["auth"])).status_code == 200
    await db.execute(update(User).where(User.username == user["username"]).values(BANNED))
    await db.commit()
    assert (await client.get("/api/auth/me", headers=user["auth"])).status_code == 401


async def test_expired_session_rejected(client: AsyncClient, db: AsyncSession) -> None:
    await _register(client)
    # created_at сдвигаем вместе с expires_at — CHECK expires_at > created_at.
    await db.execute(
        text(
            "UPDATE sessions SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 second'"
        )
    )
    await db.commit()
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_me_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


async def test_garbage_session_cookie_rejected(client: AsyncClient) -> None:
    client.cookies.set("session", "forged-token")
    assert (await client.get("/api/auth/me")).status_code == 401


# --- выход -----------------------------------------------------------------------------


async def test_logout_revokes_session_server_side(client: AsyncClient) -> None:
    await _register(client)
    old_token = client.cookies["session"]
    r = await client.post("/api/auth/logout", headers=_csrf(client))
    assert r.status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401

    # Повтор украденного/старого токена не работает: сессия удалена в БД, а не только cookie.
    client.cookies.clear()
    client.cookies.set("session", old_token)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_only_current_session(client: AsyncClient, db: AsyncSession) -> None:
    creds = await _register(client)  # сессия 1
    r = await client.post(
        "/api/auth/token", data={"username": creds["email"], "password": PASSWORD}
    )
    second = r.json()["access_token"]  # сессия 2
    first = client.cookies["session"]
    out = await client.post("/api/auth/logout", headers=_csrf(client))
    assert out.status_code == 204
    client.cookies.clear()

    client.cookies.set("session", first)
    assert (await client.get("/api/auth/me")).status_code == 401  # текущая — удалена
    client.cookies.clear()
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {second}"})
    assert me.status_code == 200  # другая — жива


# --- CSRF ------------------------------------------------------------------------------


async def test_cookie_auth_mutation_without_csrf_rejected(client: AsyncClient) -> None:
    await _register(client)
    r = await client.post("/api/auth/logout")
    assert r.status_code == 403
    assert (await client.get("/api/auth/me")).status_code == 200  # сессия не тронута


async def test_cookie_auth_mutation_with_wrong_csrf_rejected(client: AsyncClient) -> None:
    await _register(client)
    r = await client.post("/api/auth/logout", headers={"X-CSRF-Token": "attacker-guess"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/register", "/api/auth/logout"])
async def test_cross_origin_mutation_rejected(client: AsyncClient, path: str) -> None:
    r = await client.post(path, json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


async def test_same_origin_allowed(client: AsyncClient) -> None:
    r = await client.post("/api/auth/register", json=_creds(), headers={"Origin": "http://test"})
    assert r.status_code == 201


# --- Bearer (Swagger) ------------------------------------------------------------------


async def test_bearer_token_flow(client: AsyncClient) -> None:
    creds = await _register(client)
    client.cookies.clear()
    r = await client.post(
        "/api/auth/token", data={"username": creds["username"], "password": PASSWORD}
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert r.json()["token_type"] == "bearer"
    auth = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/auth/me", headers=auth)).status_code == 200
    # Bearer браузер сам не подставляет — CSRF-заголовок не нужен.
    assert (await client.post("/api/auth/logout", headers=auth)).status_code == 204
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 401


async def test_token_wrong_password(client: AsyncClient) -> None:
    creds = await _register(client)
    r = await client.post(
        "/api/auth/token", data={"username": creds["email"], "password": "nope-nope"}
    )
    assert r.status_code == 401


async def test_channel_created_once_per_user(client: AsyncClient, db: AsyncSession) -> None:
    creds = await _register(client)
    count = (
        await db.execute(
            select(Channel)
            .join(User, User.id == Channel.owner_id)
            .where(User.username == creds["username"])
        )
    ).all()
    assert len(count) == 1


# --- по итогам ревью и аудита -----------------------------------------------------------


async def test_registration_race_reports_correct_field(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гонка двух регистраций: предпроверки пропущены, срабатывает уникальный индекс.
    Ошибка определяется по имени ограничения, а не по подстроке «email» в тексте."""
    from app.repositories import users as users_repo

    async def never_taken(*_: object) -> bool:
        return False

    monkeypatch.setattr(users_repo, "email_taken", never_taken)
    monkeypatch.setattr(users_repo, "username_taken", never_taken)

    first = await _register(client, username=f"myemail_{uuid.uuid4().hex[:6]}")
    client.cookies.clear()
    same_username = await client.post("/api/auth/register", json=_creds(username=first["username"]))
    assert same_username.status_code == 409
    assert same_username.json()["detail"] == "Это имя пользователя уже занято"

    same_email = await client.post("/api/auth/register", json=_creds(email=first["email"]))
    assert same_email.status_code == 409
    assert "email" in same_email.json()["detail"]


async def test_relogin_revokes_previous_browser_session(client: AsyncClient) -> None:
    creds = await _register(client)
    old = client.cookies["session"]
    r = await client.post("/api/auth/login", json={"login": creds["email"], "password": PASSWORD})
    assert r.status_code == 200
    assert client.cookies["session"] != old

    client.cookies.clear()
    client.cookies.set("session", old)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_register_in_logged_in_browser_revokes_previous_session(
    client: AsyncClient,
) -> None:
    await _register(client)
    old = client.cookies["session"]
    await _register(client)  # второй аккаунт в том же браузере
    client.cookies.clear()
    client.cookies.set("session", old)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_is_idempotent(client: AsyncClient, db: AsyncSession) -> None:
    # Без сессии
    assert (await client.post("/api/auth/logout")).status_code == 204
    # С истёкшей сессией — 204 и cookie очищаются
    await _register(client)
    await db.execute(
        text(
            "UPDATE sessions SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 second'"
        )
    )
    await db.commit()
    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    assert "session" not in client.cookies


async def test_csrf_bound_to_session(client: AsyncClient) -> None:
    """Подложенная пара cookie csrf + заголовок с одинаковым значением не проходит:
    сервер сверяет заголовок с HMAC от токена сессии."""
    await _register(client)
    client.cookies.set("csrf", "attacker-chosen")
    r = await client.post("/api/auth/logout", headers={"X-CSRF-Token": "attacker-chosen"})
    assert r.status_code == 403


@pytest.mark.parametrize("origin", ["null", "http://test:8080", "https://test"])
async def test_cross_origin_variants_rejected(client: AsyncClient, origin: str) -> None:
    r = await client.post("/api/auth/register", json=_creds(), headers={"Origin": origin})
    assert r.status_code == 403


async def test_username_whitespace_trimmed(client: AsyncClient) -> None:
    s = uuid.uuid4().hex[:8]
    r = await client.post("/api/auth/register", json=_creds(username=f"  trim_{s}  "))
    assert r.status_code == 201
    assert r.json()["username"] == f"trim_{s}"


async def test_long_user_agent_truncated(client: AsyncClient, db: AsyncSession) -> None:
    r = await client.post("/api/auth/register", json=_creds(), headers={"User-Agent": "A" * 5000})
    assert r.status_code == 201
    ua = (await db.execute(select(UserSession.user_agent))).scalar_one()
    assert ua is not None and len(ua) == 300


async def test_token_rejects_oversized_password(client: AsyncClient) -> None:
    creds = await _register(client)
    r = await client.post(
        "/api/auth/token", data={"username": creds["email"], "password": "x" * 10_000}
    )
    assert r.status_code == 401


async def test_validation_error_detail_is_string(client: AsyncClient) -> None:
    r = await client.post("/api/auth/register", json={"email": "bad", "username": "ab"})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str)
    assert {"email", "username", "password"} <= {e["loc"][-1] for e in body["errors"]}


async def test_auth_responses_not_cached(client: AsyncClient) -> None:
    creds = await _register(client)
    me = await client.get("/api/auth/me")
    token = await client.post(
        "/api/auth/token", data={"username": creds["email"], "password": PASSWORD}
    )
    assert me.headers["cache-control"] == "no-store"
    assert token.headers["cache-control"] == "no-store"


async def test_unknown_route_error_in_russian(client: AsyncClient) -> None:
    r = await client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "Не найдено"}


@pytest.mark.parametrize("name", ["admin", "Admin", "ROOT", "moderator", "support"])
async def test_reserved_usernames_cannot_register(client: AsyncClient, name: str) -> None:
    r = await client.post("/api/auth/register", json=_creds(username=name))
    assert r.status_code == 422
