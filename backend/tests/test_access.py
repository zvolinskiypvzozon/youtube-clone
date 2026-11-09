"""Список допуска к своему видео: GET / POST {username} / DELETE …/access/{user_id}.

Права — как у правки (ADR 0006, доп. 2): не может открыть → 404, может открыть, но не
владелец → 403, аноним → 401. Лимит 100 записей считается под блокировкой строки видео.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import User, UserRole, VideoAccess
from app.repositories import videos as videos_repo
from app.services import manage as manage_service
from tests.factories import create_user
from tests.helpers import (
    MANAGE_VIEWERS,
    PRIV,
    PUB,
    api_user,
    auth_headers,
    backend_pid,
    channel_of,
    manage_expected,
    manage_matrix,
    seed_video,
    wait_for_lock_waiters,
)


def _url(video_id: uuid.UUID, user_id: str | uuid.UUID | None = None) -> str:
    base = f"/api/videos/{video_id}/access"
    return base if user_id is None else f"{base}/{user_id}"


async def _access_count(db: AsyncSession, video_id: uuid.UUID) -> int:
    n = await db.scalar(
        select(func.count()).select_from(VideoAccess).where(VideoAccess.video_id == video_id)
    )
    await db.commit()
    return n or 0


async def _fill(db: AsyncSession, video_id: uuid.UUID, n: int) -> None:
    """n пользователей в списке допуска (напрямую в БД — быстрее, чем через API)."""
    for _ in range(n):
        user = await create_user(db)
        db.add(VideoAccess(video_id=video_id, user_id=user.id))
    await db.commit()


# --- Выдача, список, отзыв ------------------------------------------------------------------


async def test_grant_list_revoke(client: AsyncClient, db: AsyncSession) -> None:
    owner, alice, bob = [await api_user(client, db) for _ in range(3)]
    video = await seed_video(db, await channel_of(db, owner), PRIV)

    r = await client.post(
        _url(video.id), json={"username": alice["username"].upper()}, headers=owner["auth"]
    )
    assert r.status_code == 201, r.text
    first = r.json()
    assert (first["user_id"], first["username"]) == (alice["id"], alice["username"])
    assert r.headers["cache-control"] == "no-store"

    # Повторная выдача — 200 и та же запись (дата не меняется).
    r = await client.post(
        _url(video.id), json={"username": f"  {alice['username']} "}, headers=owner["auth"]
    )
    assert r.status_code == 200, r.text
    assert r.json() == first

    r = await client.post(_url(video.id), json={"username": bob["username"]}, headers=owner["auth"])
    assert r.status_code == 201, r.text

    r = await client.get(_url(video.id), headers=owner["auth"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_entries"] == manage_service.ACCESS_LIMIT
    assert [e["username"] for e in body["items"]] == [bob["username"], alice["username"]]

    r = await client.delete(_url(video.id, alice["id"]), headers=owner["auth"])
    assert r.status_code == 204, r.text
    # Идемпотентно: записи уже нет, пользователя нет вовсе — тоже 204.
    for user_id in (alice["id"], uuid.uuid4()):
        r = await client.delete(_url(video.id, user_id), headers=owner["auth"])
        assert r.status_code == 204, r.text

    r = await client.get(_url(video.id), headers=owner["auth"])
    assert [e["username"] for e in r.json()["items"]] == [bob["username"]]


async def test_grant_opens_private_and_revoke_closes(client: AsyncClient, db: AsyncSession) -> None:
    owner, viewer = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV)

    async def opens() -> bool:
        card = await client.get(f"/api/videos/{video.id}", headers=viewer["auth"])
        stream = await client.get(f"/api/videos/{video.id}/stream", headers=viewer["auth"])
        assert card.status_code == stream.status_code, (card.text, stream.text)
        assert card.status_code in (200, 404), card.text
        return card.status_code == 200

    assert not await opens()
    r = await client.post(
        _url(video.id), json={"username": viewer["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 201, r.text
    assert await opens()
    r = await client.delete(_url(video.id, viewer["id"]), headers=owner["auth"])
    assert r.status_code == 204
    assert not await opens()


async def test_access_entries_are_per_video(client: AsyncClient, db: AsyncSession) -> None:
    """Записи разных видео не смешиваются: список, лимит и отзыв — только своего видео.
    X допущен к видео A и к чужому видео B (у B ещё 5 записей)."""
    owner_a, owner_b = await api_user(client, db), await api_user(client, db)
    x, newcomer = await api_user(client, db), await api_user(client, db)
    video_a = await seed_video(db, await channel_of(db, owner_a), PRIV)
    video_b = await seed_video(db, await channel_of(db, owner_b), PRIV)
    await _fill(db, video_a.id, manage_service.ACCESS_LIMIT - 2)
    await _fill(db, video_b.id, 5)
    for video, owner in ((video_a, owner_a), (video_b, owner_b)):
        r = await client.post(
            _url(video.id), json={"username": x["username"]}, headers=owner["auth"]
        )
        assert r.status_code == 201, r.text

    # Лимит A: 99 своих, записи B не считаются.
    r = await client.post(
        _url(video_a.id), json={"username": newcomer["username"]}, headers=owner_a["auth"]
    )
    assert r.status_code == 201, r.text

    r = await client.get(_url(video_a.id), headers=owner_a["auth"])
    assert len(r.json()["items"]) == manage_service.ACCESS_LIMIT

    r = await client.delete(_url(video_a.id, x["id"]), headers=owner_a["auth"])
    assert r.status_code == 204
    assert await _access_count(db, video_b.id) == 6
    r = await client.get(f"/api/videos/{video_b.id}/stream", headers=x["auth"])
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/videos/{video_a.id}", headers=x["auth"])
    assert r.status_code == 404


async def test_grant_on_public_applies_when_made_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Список ведётся при любой видимости и действует, когда видео станет приватным."""
    owner, viewer = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(
        _url(video.id), json={"username": viewer["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 201, r.text
    r = await client.patch(
        f"/api/videos/{video.id}", json={"visibility": "private"}, headers=owner["auth"]
    )
    assert r.status_code == 200, r.text
    for path in ("", "/stream"):
        r = await client.get(f"/api/videos/{video.id}{path}", headers=viewer["auth"])
        assert r.status_code == 200, (path, r.text)


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"username": "nobody_here_42"}, 404),
        ({"username": ""}, 422),
        ({"username": "   "}, 422),
        ({"username": "x" * 31}, 422),
        ({"username": "ab"}, 422),
        ({"username": "a b"}, 422),
        ({"username": "a\u0000b"}, 422),
        ({"username": "тест_ник"}, 422),
        ({}, 422),
        ({"username": "alice", "granted_at": "2020-01-01T00:00:00Z"}, 422),
    ],
)
async def test_grant_rejects(
    client: AsyncClient, db: AsyncSession, payload: dict, status: int
) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    r = await client.post(_url(video.id), json=payload, headers=owner["auth"])
    assert r.status_code == status, r.text
    assert await _access_count(db, video.id) == 0


async def test_grant_to_self_is_400(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    r = await client.post(
        _url(video.id), json={"username": owner["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 400, r.text
    assert await _access_count(db, video.id) == 0


async def test_unknown_user_and_missing_video_do_not_mix(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Чужое приватное видео и несуществующее — один и тот же 404 (до поиска пользователя)."""
    owner, stranger = await api_user(client, db), await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    for method in ("get", "post", "delete"):
        answers = []
        for video_id in (private.id, uuid.uuid4()):
            url = _url(video_id, owner["id"]) if method == "delete" else _url(video_id)
            kwargs = {"json": {"username": owner["username"]}} if method == "post" else {}
            answers.append(await getattr(client, method)(url, headers=stranger["auth"], **kwargs))
        hidden, missing = answers
        assert hidden.status_code == missing.status_code == 404, method
        assert hidden.json() == missing.json()


# --- Отказы: 401 / 403 / 404 ----------------------------------------------------------------


@pytest.mark.parametrize("viewer_kind", MANAGE_VIEWERS)
async def test_only_owner_manages_access(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    owner, target = await api_user(client, db), await api_user(client, db)
    videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
    # target уже в списке каждого видео — проверяем, что чужой DELETE его не убирает.
    db.add_all(VideoAccess(video_id=v.id, user_id=uuid.UUID(target["id"])) for v in videos.values())
    await db.commit()
    headers = auth_headers(viewer)
    stranger_name = (await api_user(client, db))["username"]

    for name, video in videos.items():
        expected = manage_expected(viewer_kind, name)
        before = await _access_count(db, video.id)
        responses = [
            await client.get(_url(video.id), headers=headers),
            await client.post(_url(video.id), json={"username": stranger_name}, headers=headers),
            await client.delete(_url(video.id, target["id"]), headers=headers),
        ]
        for r in responses:
            assert r.status_code == expected, (name, r.request.method, r.text)
            assert "items" not in r.json()
        assert await _access_count(db, video.id) == before, name


@pytest.mark.parametrize("method", ["post", "delete"])
async def test_access_via_cookie_requires_csrf(
    client: AsyncClient, db: AsyncSession, method: str
) -> None:
    s = uuid.uuid4().hex[:8]
    r = await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    owner = {"id": r.json()["id"]}
    target = await create_user(db)
    await db.commit()
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    if method == "delete":
        db.add(VideoAccess(video_id=video.id, user_id=target.id))
        await db.commit()

    async def call(headers: dict) -> int:
        if method == "post":
            r = await client.post(
                _url(video.id), json={"username": target.username}, headers=headers
            )
        else:
            r = await client.delete(_url(video.id, target.id), headers=headers)
        return r.status_code

    assert await call({}) == 403
    assert await _access_count(db, video.id) == (1 if method == "delete" else 0)
    assert await call({"X-CSRF-Token": client.cookies["csrf"]}) in (201, 204)
    assert await _access_count(db, video.id) == (0 if method == "delete" else 1)


# --- Лимит ----------------------------------------------------------------------------------


async def test_access_limit(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    await _fill(db, video.id, manage_service.ACCESS_LIMIT - 1)
    last, extra = await api_user(client, db), await api_user(client, db)

    r = await client.post(
        _url(video.id), json={"username": last["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        _url(video.id), json={"username": extra["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 409, r.text
    # Уже допущенный при полном списке — 200, а не 409.
    r = await client.post(
        _url(video.id), json={"username": last["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 200, r.text
    # После отзыва место освобождается.
    r = await client.delete(_url(video.id, last["id"]), headers=owner["auth"])
    assert r.status_code == 204
    r = await client.post(
        _url(video.id), json={"username": extra["username"]}, headers=owner["auth"]
    )
    assert r.status_code == 201, r.text
    assert await _access_count(db, video.id) == manage_service.ACCESS_LIMIT


async def test_access_limit_race(client: AsyncClient, db: AsyncSession) -> None:
    """99 записей, две выдачи одновременно. Первая (настоящий сервис в своей сессии) добавила
    сотую и держит транзакцию; вторая обязана дождаться её commit и получить 409. Без
    блокировки строки видео вторая увидела бы 99 закоммиченных записей и добавила 101-ю."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    await _fill(db, video.id, manage_service.ACCESS_LIMIT - 1)
    first, second = await api_user(client, db), await api_user(client, db)

    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as grant_db:
        grant_pid = await backend_pid(grant_db)
        real_commit = grant_db.commit

        async def gated_commit() -> None:
            holding.set()  # сотая запись вставлена, строка видео заблокирована
            await release.wait()
            await real_commit()

        grant_db.commit = gated_commit  # type: ignore[method-assign]
        viewer = await grant_db.get(User, uuid.UUID(owner["id"]))
        grant = asyncio.create_task(
            manage_service.grant_access(grant_db, viewer, video.id, first["username"])
        )
        await asyncio.wait_for(holding.wait(), timeout=5)

        competing = asyncio.create_task(
            client.post(
                _url(video.id), json={"username": second["username"]}, headers=owner["auth"]
            )
        )
        await wait_for_lock_waiters(grant_pid)
        release.set()
        _, created = await grant
        assert created
        r = await competing

    assert r.status_code == 409, r.text
    assert await _access_count(db, video.id) == manage_service.ACCESS_LIMIT


@pytest.mark.parametrize("role", [UserRole.user, UserRole.moderator])
async def test_denied_grant_does_not_wait_for_row_lock(
    client: AsyncClient, db: AsyncSession, role: UserRole
) -> None:
    """Права проверяются до блокировки: чужой POST не встаёт в очередь на строку видео."""
    owner, other = await api_user(client, db), await api_user(client, db, role)
    target = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async with get_sessionmaker()() as worker_db:
        assert await videos_repo.lock_video(worker_db, video.id) is not None
        r = await asyncio.wait_for(
            client.post(
                _url(video.id), json={"username": target["username"]}, headers=other["auth"]
            ),
            timeout=3,
        )
        assert r.status_code == 403, r.text
        await worker_db.rollback()
