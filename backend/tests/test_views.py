"""Просмотры: POST /api/videos/{id}/view.

Доступ — can_view (иначе 404, в том числе анониму). Засчитывается только готовое и не скрытое
видео, и только если в момент записи зритель всё ещё может его видеть (условия политики в
самом UPDATE, ADR 0006 доп. 4); дедупликации нет (упрощение дня 5).
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import User, Video
from app.schemas.videos import VideoUpdateIn
from app.services import manage as manage_service
from tests.helpers import (
    MANAGE_OPENS,
    MANAGE_STATES,
    MANAGE_VIEWERS,
    PRIV,
    PUB,
    api_user,
    auth_headers,
    backend_pid,
    channel_of,
    manage_matrix,
    seed_video,
    wait_for_lock_waiters,
)

NOT_READY_STATES = {"processing", "failed"}


def _url(video_id: uuid.UUID) -> str:
    return f"/api/videos/{video_id}/view"


async def _views(db: AsyncSession, video_id: uuid.UUID) -> int:
    n = await db.scalar(select(Video.views_count).where(Video.id == video_id))
    await db.commit()
    return n or 0


async def test_view_counts_for_anonymous_and_users(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    for headers in ({}, user["auth"], owner["auth"], user["auth"]):  # без дедупликации
        r = await client.post(_url(video.id), headers=headers)
        assert r.status_code == 204, r.text
    assert await _views(db, video.id) == 4
    card = (await client.get(f"/api/videos/{video.id}")).json()
    assert card["views_count"] == 4
    # Просмотр не меняет время правки метаданных.
    updated = await db.scalar(select(Video.updated_at).where(Video.id == video.id))
    await db.commit()
    assert updated == video.updated_at


@pytest.mark.parametrize("viewer_kind", [*MANAGE_VIEWERS, "owner"])
async def test_view_access_matrix(client: AsyncClient, db: AsyncSession, viewer_kind: str) -> None:
    """Не может открыть — 404 (и аноним тоже: вход не нужен); не готово — 409; скрытое
    модератором владелец и staff открывают, но просмотр не засчитывается (204)."""
    owner = await api_user(client, db)
    if viewer_kind == "owner":
        videos, _ = await manage_matrix(client, db, owner, "anon")
        viewer: dict | None = owner
        opens = set(MANAGE_STATES)
    else:
        videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
        opens = MANAGE_OPENS.get(viewer_kind, {"public", "unlisted"})  # аноним — как stranger

    for name, video in videos.items():
        r = await client.post(_url(video.id), headers=auth_headers(viewer))
        if name not in opens:
            expected, counted = 404, 0
        elif name in NOT_READY_STATES:
            expected, counted = 409, 0
        else:
            expected, counted = 204, 0 if name == "hidden" else 1
        assert r.status_code == expected, (name, r.text)
        assert await _views(db, video.id) == counted, name


async def test_not_found_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    hidden = await client.post(_url(private.id))
    missing = await client.post(_url(uuid.uuid4()))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert await _views(db, private.id) == 0


async def test_view_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    s = uuid.uuid4().hex[:8]
    await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    assert (await client.post(_url(video.id))).status_code == 403
    r = await client.post(_url(video.id), headers={"X-CSRF-Token": client.cookies["csrf"]})
    assert r.status_code == 204, r.text
    assert await _views(db, video.id) == 1


async def test_concurrent_views_are_not_lost(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    statuses = await asyncio.gather(*(client.post(_url(video.id)) for _ in range(20)))
    assert {r.status_code for r in statuses} == {204}
    assert await _views(db, video.id) == 20


async def test_view_not_counted_if_made_private_while_waiting(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Владелец делает видео приватным (PATCH держит строку, commit придержан), аноним в это
    время шлёт просмотр: права проверены до записи, но UPDATE ждёт блокировку и проверяет
    условие (visible_filter) заново по новой версии строки — просмотр не засчитан."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as patch_db:
        patch_pid = await backend_pid(patch_db)
        real_commit = patch_db.commit

        async def gated_commit() -> None:
            holding.set()
            await release.wait()
            await real_commit()

        patch_db.commit = gated_commit  # type: ignore[method-assign]
        me = await patch_db.get(User, uuid.UUID(owner["id"]))
        patch = asyncio.create_task(
            manage_service.update_video(patch_db, me, video.id, VideoUpdateIn(visibility=PRIV))
        )
        await asyncio.wait_for(holding.wait(), timeout=5)
        view = asyncio.create_task(client.post(_url(video.id)))
        await wait_for_lock_waiters(patch_pid)
        release.set()
        await patch
        r = await view

    assert r.status_code == 204, r.text
    assert await _views(db, video.id) == 0
