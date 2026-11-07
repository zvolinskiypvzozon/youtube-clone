"""Роли: require_role, смена роли администратором, первый администратор при деплое."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap_admin import BootstrapError, ensure_admin
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.errors import Forbidden
from app.core.security import verify_password
from app.models import Channel, User, UserRole
from app.repositories import users as users_repo
from app.services import admin as admin_service

PASSWORD = "role-test-password"


async def _account(client: AsyncClient, db: AsyncSession, role: UserRole) -> dict:
    """Регистрирует пользователя, выставляет роль в БД, возвращает id и Bearer-заголовок."""
    s = uuid.uuid4().hex[:8]
    creds = {"email": f"r_{s}@example.com", "username": f"r_{s}", "password": PASSWORD}
    r = await client.post("/api/auth/register", json=creds)
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    if role != UserRole.user:
        await db.execute(update(User).where(User.id == uuid.UUID(user_id)).values(role=role))
        await db.commit()
    client.cookies.clear()
    tok = await client.post(
        "/api/auth/token", data={"username": creds["username"], "password": PASSWORD}
    )
    return {"id": user_id, "auth": {"Authorization": f"Bearer {tok.json()['access_token']}"}}


# --- require_role: матрица доступа ------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected",
    [(None, 401), (UserRole.user, 403), (UserRole.moderator, 403), (UserRole.admin, 200)],
)
async def test_admin_users_list_access_matrix(
    client: AsyncClient, db: AsyncSession, role: UserRole | None, expected: int
) -> None:
    headers = (await _account(client, db, role))["auth"] if role else {}
    r = await client.get("/api/admin/users", headers=headers)
    assert r.status_code == expected


async def test_admin_users_list_content(client: AsyncClient, db: AsyncSession) -> None:
    admin = await _account(client, db, UserRole.admin)
    other = await _account(client, db, UserRole.user)
    r = await client.get("/api/admin/users?limit=10", headers=admin["auth"])
    body = r.json()
    ids = {u["id"] for u in body["items"]}
    assert {admin["id"], other["id"]} <= ids
    assert body["total"] >= 2
    assert "password_hash" not in r.text


@pytest.mark.parametrize("limit", [0, 101])
async def test_admin_users_list_limit_bounds(
    client: AsyncClient, db: AsyncSession, limit: int
) -> None:
    admin = await _account(client, db, UserRole.admin)
    r = await client.get(f"/api/admin/users?limit={limit}", headers=admin["auth"])
    assert r.status_code == 422


# --- смена роли ------------------------------------------------------------------------


@pytest.mark.parametrize("actor_role", [UserRole.user, UserRole.moderator])
async def test_non_admin_cannot_escalate_own_role(
    client: AsyncClient, db: AsyncSession, actor_role: UserRole
) -> None:
    actor = await _account(client, db, actor_role)
    r = await client.patch(
        f"/api/admin/users/{actor['id']}", json={"role": "admin"}, headers=actor["auth"]
    )
    assert r.status_code == 403
    role = await db.scalar(select(User.role).where(User.id == uuid.UUID(actor["id"])))
    assert role == actor_role


async def test_moderator_cannot_promote_others(client: AsyncClient, db: AsyncSession) -> None:
    moderator = await _account(client, db, UserRole.moderator)
    target = await _account(client, db, UserRole.user)
    r = await client.patch(
        f"/api/admin/users/{target['id']}", json={"role": "moderator"}, headers=moderator["auth"]
    )
    assert r.status_code == 403


async def test_admin_promotes_user_and_it_applies_immediately(
    client: AsyncClient, db: AsyncSession
) -> None:
    admin = await _account(client, db, UserRole.admin)
    target = await _account(client, db, UserRole.user)

    r = await client.patch(
        f"/api/admin/users/{target['id']}", json={"role": "moderator"}, headers=admin["auth"]
    )
    assert r.status_code == 200 and r.json()["role"] == "moderator"
    # Роль читается из БД на каждом запросе — действует без повторного входа.
    me = await client.get("/api/auth/me", headers=target["auth"])
    assert me.json()["role"] == "moderator"


async def test_demoted_admin_loses_access_immediately(
    client: AsyncClient, db: AsyncSession
) -> None:
    admin = await _account(client, db, UserRole.admin)
    second = await _account(client, db, UserRole.admin)
    r = await client.patch(
        f"/api/admin/users/{second['id']}", json={"role": "user"}, headers=admin["auth"]
    )
    assert r.status_code == 200
    assert (await client.get("/api/admin/users", headers=second["auth"])).status_code == 403


@pytest.mark.parametrize("new_role", ["user", "moderator"])
async def test_admin_cannot_demote_self(
    client: AsyncClient, db: AsyncSession, new_role: str
) -> None:
    admin = await _account(client, db, UserRole.admin)
    await _account(client, db, UserRole.admin)  # второй админ: проверяем именно «себя»
    r = await client.patch(
        f"/api/admin/users/{admin['id']}", json={"role": new_role}, headers=admin["auth"]
    )
    assert r.status_code == 409
    assert (await client.get("/api/admin/users", headers=admin["auth"])).status_code == 200


async def test_set_role_unknown_user(client: AsyncClient, db: AsyncSession) -> None:
    admin = await _account(client, db, UserRole.admin)
    r = await client.patch(
        f"/api/admin/users/{uuid.uuid4()}", json={"role": "moderator"}, headers=admin["auth"]
    )
    assert r.status_code == 404


async def test_set_role_invalid_value(client: AsyncClient, db: AsyncSession) -> None:
    admin = await _account(client, db, UserRole.admin)
    target = await _account(client, db, UserRole.user)
    r = await client.patch(
        f"/api/admin/users/{target['id']}", json={"role": "superuser"}, headers=admin["auth"]
    )
    assert r.status_code == 422


async def test_role_change_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    """Браузерный путь: cookie-сессия администратора без CSRF-заголовка — 403."""
    s = uuid.uuid4().hex[:8]
    creds = {"email": f"c_{s}@example.com", "username": f"c_{s}", "password": PASSWORD}
    admin_id = (await client.post("/api/auth/register", json=creds)).json()["id"]
    await db.execute(update(User).where(User.id == uuid.UUID(admin_id)).values(role="admin"))
    await db.commit()
    target = (
        await client.post(
            "/api/auth/register",
            json={**creds, "email": f"t_{s}@example.com", "username": f"t_{s}"},
        )
    ).json()["id"]
    # После второй регистрации в браузере — сессия target; вернёмся к администратору.
    await client.post("/api/auth/login", json={"login": creds["username"], "password": PASSWORD})

    r = await client.patch(f"/api/admin/users/{target}", json={"role": "moderator"})
    assert r.status_code == 403
    r = await client.patch(
        f"/api/admin/users/{target}",
        json={"role": "moderator"},
        headers={"X-CSRF-Token": client.cookies["csrf"]},
    )
    assert r.status_code == 200


# --- первый администратор (bootstrap при деплое) ---------------------------------------


async def test_bootstrap_admin_is_idempotent(db: AsyncSession) -> None:
    url = get_settings().database_url
    s = uuid.uuid4().hex[:8]
    username, email = f"boot_{s}", f"boot_{s}@example.com"

    assert await ensure_admin(url, username, email, "first-password") is True
    # Повторный деплой с другим паролем в .env не перезаписывает существующего администратора.
    assert await ensure_admin(url, username, email, "second-password") is False

    users = (await db.execute(select(User).where(User.username == username))).scalars().all()
    assert len(users) == 1
    admin = users[0]
    assert admin.role == UserRole.admin
    assert verify_password(admin.password_hash, "first-password")
    channels = await db.scalar(
        select(func.count()).select_from(Channel).where(Channel.owner_id == admin.id)
    )
    assert channels == 1


@pytest.mark.parametrize(
    "username,email,password",
    [
        ("short_pw", "s@example.com", "123"),
        ("bad name", "s@example.com", "long-enough-pw"),
        ("good_name", "not-an-email", "long-enough-pw"),
    ],
)
async def test_bootstrap_admin_validates_input(username: str, email: str, password: str) -> None:
    with pytest.raises(BootstrapError):
        await ensure_admin(get_settings().database_url, username, email, password)


async def test_bootstrap_refuses_existing_non_admin(client: AsyncClient, db: AsyncSession) -> None:
    """Имя администратора занято обычным пользователем → ошибка, а не «уже существует»."""
    regular = await _account(client, db, UserRole.user)
    username = await db.scalar(select(User.username).where(User.id == uuid.UUID(regular["id"])))
    with pytest.raises(BootstrapError):
        await ensure_admin(
            get_settings().database_url,
            username,
            f"new_{uuid.uuid4().hex[:6]}@example.com",
            "long-enough-pw",
        )
    role = await db.scalar(select(User.role).where(User.id == uuid.UUID(regular["id"])))
    assert role == UserRole.user  # роль чужого аккаунта не изменилась


async def test_bootstrap_refuses_email_of_other_account(
    client: AsyncClient, db: AsyncSession
) -> None:
    other = await _account(client, db, UserRole.admin)
    email = await db.scalar(select(User.email).where(User.id == uuid.UUID(other["id"])))
    with pytest.raises(BootstrapError):
        await ensure_admin(
            get_settings().database_url, f"boot_{uuid.uuid4().hex[:6]}", email, "long-enough-pw"
        )


# --- инвариант «хотя бы один администратор» ----------------------------------------------
# Через API его обеспечивают: блокировка смены ролей + повторное чтение роли инициатора +
# запрет самопонижения. Проверка count_admins в сервисе — защита на будущее (бан, удаление).


async def test_concurrent_mutual_demotion_is_serialized(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Два администратора одновременно понижают друг друга.

    Детерминированно воспроизводим гонку двумя независимыми транзакциями:
    T1 взяла блокировку и понизила B, но ещё не закоммитила. T2 (B, чья роль в памяти — admin)
    пытается понизить A. Без блокировки T2 прочитала бы роль B до commit T1, прошла проверку,
    и администраторов не осталось бы. С блокировкой T2 ждёт, затем перечитывает роль B → 403.
    """
    a = await _account(client, db, UserRole.admin)
    b = await _account(client, db, UserRole.admin)
    a_id, b_id = uuid.UUID(a["id"]), uuid.UUID(b["id"])

    maker = get_sessionmaker()
    async with maker() as t1, maker() as t2:
        b_seen_by_t2 = await t2.get(User, b_id)  # роль admin «в памяти» T2
        await t2.commit()

        await users_repo.lock_role_changes(t1)
        target = await t1.get(User, b_id)
        target.role = UserRole.user
        await t1.flush()  # T1 держит блокировку, не закоммичена

        t2_task = asyncio.create_task(admin_service.set_role(t2, b_seen_by_t2, a_id, UserRole.user))
        await asyncio.sleep(0.5)
        assert not t2_task.done(), "T2 должна ждать блокировку смены ролей"

        await t1.commit()
        with pytest.raises(Forbidden):
            await t2_task

    admins = await db.scalar(
        select(func.count()).select_from(User).where(User.role == UserRole.admin)
    )
    assert admins == 1


async def test_admin_list_not_cached(client: AsyncClient, db: AsyncSession) -> None:
    admin = await _account(client, db, UserRole.admin)
    r = await client.get("/api/admin/users", headers=admin["auth"])
    assert r.headers["cache-control"] == "no-store"
