"""Управление своими видео: правка метаданных и видимости.

Права: только владелец (policies.can_manage). Отказ — 404, если видео нельзя даже открыть
(can_view), и 403, если открыть можно, а управлять нельзя.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import User, UserRole, Video, VideoAccess, VideoStatus, VideoVisibility
from app.repositories import jobs as jobs_repo
from app.repositories import videos as videos_repo
from app.schemas.videos import VideoUpdateIn
from app.services import manage as manage_service
from app.services.publication import publish_if_due
from app.worker import media, processor
from tests.helpers import backend_pid, wait_for_lock_waiters
from tests.test_videos import (
    FAIL,
    HIDDEN,
    OK,
    PRIV,
    PROC,
    PUB,
    READY,
    UNL,
    _auth,
    _channel,
    _user,
    _video,
)


async def _fresh(db: AsyncSession, video_id: uuid.UUID) -> Video:
    stmt = select(Video).where(Video.id == video_id).execution_options(populate_existing=True)
    video = (await db.execute(stmt)).scalar_one()
    await db.commit()
    return video


async def _feed_ids(client: AsyncClient) -> set[str]:
    r = await client.get("/api/videos")
    assert r.status_code == 200
    return {item["id"] for item in r.json()["items"]}


# --- Правка владельцем ----------------------------------------------------------------------


async def test_owner_updates_metadata(client: AsyncClient, db: AsyncSession) -> None:
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PUB)

    r = await client.patch(
        f"/api/videos/{video.id}",
        json={"title": "  Новое название  ", "description": "Описание", "visibility": "unlisted"},
        headers=owner["auth"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Новое название"
    assert body["description"] == "Описание"
    assert body["state"]["visibility"] == "unlisted"
    assert r.headers["cache-control"] == "no-store"

    # Не переданные поля не меняются.
    r = await client.patch(
        f"/api/videos/{video.id}", json={"description": ""}, headers=owner["auth"]
    )
    assert r.status_code == 200
    stored = await _fresh(db, video.id)
    assert (stored.title, stored.description, stored.visibility) == ("Новое название", "", UNL)


async def test_private_to_public_publishes_once(client: AsyncClient, db: AsyncSession) -> None:
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PRIV)
    assert video.published_at is None
    assert str(video.id) not in await _feed_ids(client)

    async def set_visibility(value: str) -> None:
        r = await client.patch(
            f"/api/videos/{video.id}", json={"visibility": value}, headers=owner["auth"]
        )
        assert r.status_code == 200, r.text

    await set_visibility("public")
    published = (await _fresh(db, video.id)).published_at
    assert published is not None
    assert str(video.id) in await _feed_ids(client)

    await set_visibility("private")
    assert str(video.id) not in await _feed_ids(client)
    await set_visibility("public")
    # Повторная публикация не поднимает видео в ленте: дата первой публикации.
    assert (await _fresh(db, video.id)).published_at == published
    assert str(video.id) in await _feed_ids(client)


@pytest.mark.parametrize("status", [PROC, FAIL, VideoStatus.uploading])
async def test_visibility_before_ready_does_not_publish(
    client: AsyncClient, db: AsyncSession, status: VideoStatus
) -> None:
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PRIV, status)
    r = await client.patch(
        f"/api/videos/{video.id}", json={"visibility": "public"}, headers=owner["auth"]
    )
    assert r.status_code == 200, r.text
    stored = await _fresh(db, video.id)
    assert stored.visibility == PUB and stored.published_at is None


async def test_owner_edits_hidden_video_but_it_stays_out_of_feed(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PRIV, READY, HIDDEN)
    r = await client.patch(
        f"/api/videos/{video.id}", json={"visibility": "public"}, headers=owner["auth"]
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"]["moderation"] == "hidden"
    assert str(video.id) not in await _feed_ids(client)


# --- Правило публикации ---------------------------------------------------------------------


@pytest.mark.parametrize("status", list(VideoStatus))
@pytest.mark.parametrize("visibility", list(VideoVisibility))
def test_publish_if_due(status: VideoStatus, visibility: VideoVisibility) -> None:
    video = Video(status=status, visibility=visibility, published_at=None)
    publish_if_due(video)
    due = status == READY and visibility != PRIV
    assert (video.published_at is not None) == due

    earlier = datetime.now(UTC) - timedelta(days=1)
    video.published_at = earlier
    publish_if_due(video)
    assert video.published_at == earlier


# --- Отказы: 401 / 403 / 404 ----------------------------------------------------------------

STATES = {
    "public": (PUB, READY, OK),
    "unlisted": (UNL, READY, OK),
    "private": (PRIV, READY, OK),
    "hidden": (PUB, READY, HIDDEN),
    "processing": (PUB, PROC, OK),
    "failed": (PUB, FAIL, OK),
}
# Кто может открыть видео (can_view) — но не управлять: 403. Остальным — 404.
OPENS = {
    "stranger": {"public", "unlisted"},
    "granted": {"public", "unlisted", "private"},
    "moderator": set(STATES),
    "admin": set(STATES),
}


@pytest.mark.parametrize("viewer_kind", [*OPENS, "anon"])
async def test_only_owner_can_update(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    owner = await _user(client, db)
    channel = await _channel(db, owner)
    videos = {name: await _video(db, channel, *state) for name, state in STATES.items()}
    roles = {"moderator": UserRole.moderator, "admin": UserRole.admin}
    viewer = (
        None
        if viewer_kind == "anon"
        else await _user(client, db, roles.get(viewer_kind, UserRole.user))
    )
    if viewer_kind == "granted":
        db.add_all(
            VideoAccess(video_id=v.id, user_id=uuid.UUID(viewer["id"])) for v in videos.values()
        )
        await db.commit()

    for name, video in videos.items():
        r = await client.patch(
            f"/api/videos/{video.id}", json={"title": "Взлом"}, headers=_auth(viewer)
        )
        expected = 401 if viewer is None else 403 if name in OPENS[viewer_kind] else 404
        assert r.status_code == expected, (name, r.text)
        assert (await _fresh(db, video.id)).title != "Взлом"


async def test_not_found_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await _user(client, db), await _user(client, db)
    private = await _video(db, await _channel(db, owner), PRIV)
    hidden = await client.patch(
        f"/api/videos/{private.id}", json={"title": "x"}, headers=stranger["auth"]
    )
    missing = await client.patch(
        f"/api/videos/{uuid.uuid4()}", json={"title": "x"}, headers=stranger["auth"]
    )
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": None},
        {"visibility": None},
        {"description": None},
        {"title": "   "},
        {"title": "x" * 101},
        {"description": "x" * 5001},
        {"visibility": "secret"},
        {"status": "ready"},
        {"moderation": "ok"},
        {"published_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_update_validation(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PRIV)
    r = await client.patch(f"/api/videos/{video.id}", json=payload, headers=owner["auth"])
    assert r.status_code == 422, (payload, r.text)
    stored = await _fresh(db, video.id)
    assert (stored.title, stored.visibility, stored.published_at) == (video.title, PRIV, None)


async def test_update_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    s = uuid.uuid4().hex[:8]
    r = await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    owner = {"id": r.json()["id"]}
    video = await _video(db, await _channel(db, owner), PRIV)

    r = await client.patch(f"/api/videos/{video.id}", json={"title": "Без CSRF"})
    assert r.status_code == 403
    r = await client.patch(
        f"/api/videos/{video.id}",
        json={"title": "С CSRF"},
        headers={"X-CSRF-Token": client.cookies["csrf"]},
    )
    assert r.status_code == 200, r.text
    assert (await _fresh(db, video.id)).title == "С CSRF"


# --- Гонка с финализацией worker-а ----------------------------------------------------------


async def test_update_waits_for_finalize_and_publishes(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Worker финализирует приватное видео (строка заблокирована), владелец в это время
    делает его публичным. Правка обязана дождаться commit и увидеть status=ready —
    иначе видео станет ready + public без published_at и никогда не попадёт в ленту.

    Сторона worker-а здесь повторяет _finalize вручную (lock_video → ready → publish_if_due);
    что настоящий _finalize вызывает publish_if_due, закрепляет test_worker (мутация M7)."""
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PRIV, PROC)

    async with get_sessionmaker()() as worker_db:
        locked = await videos_repo.lock_video(worker_db, video.id)
        assert locked is not None
        worker_pid = await backend_pid(worker_db)
        patch = asyncio.create_task(
            client.patch(
                f"/api/videos/{video.id}", json={"visibility": "public"}, headers=owner["auth"]
            )
        )
        await wait_for_lock_waiters(worker_pid)
        locked.status = READY
        publish_if_due(locked)  # как в _finalize: приватное — не публикуется
        assert locked.published_at is None
        await worker_db.commit()

    r = await patch
    assert r.status_code == 200, r.text
    stored = await _fresh(db, video.id)
    assert (stored.status, stored.visibility) == (READY, PUB)
    assert stored.published_at is not None
    assert str(video.id) in await _feed_ids(client)


@pytest.mark.parametrize("role", [UserRole.user, UserRole.moderator])
async def test_denied_update_does_not_wait_for_row_lock(
    client: AsyncClient, db: AsyncSession, role: UserRole
) -> None:
    """Права проверяются до блокировки: чужой PATCH не встаёт в очередь на строку видео
    (иначе поток чужих запросов держал бы соединения пула и тормозил владельца и worker)."""
    owner, other = await _user(client, db), await _user(client, db, role)
    video = await _video(db, await _channel(db, owner), PUB)

    async with get_sessionmaker()() as worker_db:
        assert await videos_repo.lock_video(worker_db, video.id) is not None
        r = await asyncio.wait_for(
            client.patch(f"/api/videos/{video.id}", json={"title": "x"}, headers=other["auth"]),
            timeout=3,
        )
        assert r.status_code == 403, r.text
        await worker_db.rollback()


async def test_finalize_waits_for_update_and_publishes(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Обратный порядок: владелец делает приватное необработанное видео публичным (строка
    заблокирована правкой, commit ещё не сделан), в это время worker финализирует. Настоящий
    `_finalize` обязан дождаться commit правки и увидеть visibility=public — иначе видео
    станет ready + public без published_at. Обе стороны — настоящий код: сервис правки
    и финализация worker-а, каждая в своей сессии БД."""
    owner = await _user(client, db)
    video = await _video(db, await _channel(db, owner), PRIV, PROC)
    await videos_repo.enqueue_processing(db, video.id)
    await db.commit()
    async with get_sessionmaker()() as s:
        claimed = await jobs_repo.claim(s, 900)
        await s.commit()
    assert claimed is not None and claimed.video_id == video.id

    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as patch_db:
        patch_pid = await backend_pid(patch_db)
        real_commit = patch_db.commit

        async def gated_commit() -> None:
            holding.set()  # строка заблокирована, изменения внесены — держим транзакцию
            await release.wait()
            await real_commit()

        patch_db.commit = gated_commit  # type: ignore[method-assign]
        viewer = await patch_db.get(User, uuid.UUID(owner["id"]))
        patch = asyncio.create_task(
            manage_service.update_video(patch_db, viewer, video.id, VideoUpdateIn(visibility=PUB))
        )
        await asyncio.wait_for(holding.wait(), timeout=5)

        probe = media.Probe(duration=5.0, width=320, height=240, has_audio=True)
        finalize = asyncio.create_task(processor._finalize(claimed, probe, [], []))
        await wait_for_lock_waiters(patch_pid)
        release.set()
        card = await patch
        assert card.state is not None and card.state.visibility == PUB
        await finalize

    stored = await _fresh(db, video.id)
    assert (stored.status, stored.visibility) == (READY, PUB)
    assert stored.published_at is not None
    assert str(video.id) in await _feed_ids(client)
