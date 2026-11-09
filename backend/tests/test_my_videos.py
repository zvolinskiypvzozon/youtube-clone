"""«Мои видео»: GET /api/me/videos — свои видео в любом статусе, новые загрузки сверху.

Правило — policies.owned_filter: только канал зрителя, и для модераторов/админов тоже
(чужие видео они видят в модерации, а не здесь).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, Video, VideoStatus
from app.repositories import videos as videos_repo
from app.services import cursor, policies
from tests.helpers import (
    MANAGE_STATES,
    OK,
    PRIV,
    PUB,
    READY,
    api_user,
    channel_of,
    generic_plan,
    manage_matrix,
    seed_video,
)

URL = "/api/me/videos"
BASE = datetime(2026, 11, 1, tzinfo=UTC)


async def _set_created(db: AsyncSession, video_id: uuid.UUID, at: datetime) -> None:
    await db.execute(update(Video).where(Video.id == video_id).values(created_at=at))
    await db.commit()


async def _all_pages(client: AsyncClient, headers: dict, limit: int) -> list[dict]:
    items, cursor = [], None
    while True:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        r = await client.get(URL, params=params, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) <= limit
        items += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return items


async def test_owner_sees_all_own_videos_newest_first(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, other = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    states = {**MANAGE_STATES, "uploading": (PRIV, VideoStatus.uploading, OK)}
    videos = {}
    for i, (name, state) in enumerate(states.items()):
        videos[name] = await seed_video(db, channel, *state)
        await _set_created(db, videos[name].id, BASE + timedelta(minutes=i))
    foreign = await seed_video(db, await channel_of(db, other), PUB)

    r = await client.get(URL, headers=owner["auth"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    items = r.json()["items"]
    expected_order = [str(videos[name].id) for name in reversed(states)]
    assert [i["id"] for i in items] == expected_order
    assert str(foreign.id) not in {i["id"] for i in items}

    by_id = {i["id"]: i for i in items}
    hidden, private = by_id[str(videos["hidden"].id)], by_id[str(videos["private"].id)]
    assert hidden["state"]["moderation"] == "hidden"
    assert private["state"]["visibility"] == "private" and private["published_at"] is None
    assert by_id[str(videos["uploading"].id)]["state"]["status"] == "uploading"
    # Превью есть только у готовых (seed_video кладёт файлы для ready).
    for name, video in videos.items():
        has_thumb = by_id[str(video.id)]["thumbnail_url"] is not None
        assert has_thumb == (states[name][1] == READY), name

    # Другой пользователь не видит чужие приватные и скрытые видео в своих.
    r = await client.get(URL, headers=other["auth"])
    assert [i["id"] for i in r.json()["items"]] == [str(foreign.id)]


@pytest.mark.parametrize("viewer_kind", ["granted", "moderator", "admin"])
async def test_foreign_videos_never_listed(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    """Чужие видео во всех состояниях не попадают в «Мои видео» ни допущенному (ветка
    granted у visible_filter), ни модератору и админу (они видят всё через can_view)."""
    owner = await api_user(client, db)
    _, viewer = await manage_matrix(client, db, owner, viewer_kind)
    assert viewer is not None
    own = await seed_video(db, await channel_of(db, viewer), PRIV)
    r = await client.get(URL, headers=viewer["auth"])
    assert [i["id"] for i in r.json()["items"]] == [str(own.id)]


async def test_cursor_pointing_at_foreign_video_lists_only_own(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, other = await api_user(client, db), await api_user(client, db)
    foreign = await seed_video(db, await channel_of(db, other), PRIV, media=False)
    await _set_created(db, foreign.id, BASE + timedelta(days=1))
    own = await seed_video(db, await channel_of(db, owner), PRIV, media=False)
    await _set_created(db, own.id, BASE)
    forged = cursor.encode(BASE + timedelta(days=1), foreign.id)
    r = await client.get(URL, params={"cursor": forged}, headers=owner["auth"])
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["items"]] == [str(own.id)]


async def test_full_last_page_has_no_cursor(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    for _ in range(4):
        await seed_video(db, channel, PUB, media=False)
    first = (await client.get(URL, params={"limit": 2}, headers=owner["auth"])).json()
    second = (
        await client.get(
            URL, params={"limit": 2, "cursor": first["next_cursor"]}, headers=owner["auth"]
        )
    ).json()
    assert len(second["items"]) == 2 and second["next_cursor"] is None


async def test_pagination_with_equal_created_at(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    ids = []
    for i in range(7):
        v = await seed_video(db, channel, PUB, media=False)
        # Пары с одинаковым created_at: порядок внутри — по id.
        await _set_created(db, v.id, BASE + timedelta(minutes=i // 2))
        ids.append(v.id)

    items = await _all_pages(client, owner["auth"], 2)
    got = [uuid.UUID(i["id"]) for i in items]
    assert len(got) == len(set(got)) == 7
    keys = [(datetime.fromisoformat(i["created_at"]), uuid.UUID(i["id"])) for i in items]
    assert keys == sorted(keys, reverse=True)


async def test_requires_login_and_valid_cursor(client: AsyncClient, db: AsyncSession) -> None:
    assert (await client.get(URL)).status_code == 401
    owner = await api_user(client, db)
    r = await client.get(URL, params={"cursor": "not-a-cursor"}, headers=owner["auth"])
    assert r.status_code == 400
    r = await client.get(URL, params={"limit": 51}, headers=owner["auth"])
    assert r.status_code == 422


@pytest.mark.parametrize("with_cursor", [False, True])
async def test_my_videos_use_channel_created_index(
    client: AsyncClient, db: AsyncSession, with_cursor: bool
) -> None:
    owner = await api_user(client, db)
    viewer = await db.get(User, uuid.UUID(owner["id"]))
    after = (datetime.now(UTC), uuid.uuid4()) if with_cursor else None
    stmt = videos_repo.own_page_stmt(policies.owned_filter(viewer), after, 25)
    plan = await generic_plan(db, stmt)
    assert "ix_videos_channel_created" in plan, plan
    assert "Sort" not in plan, plan  # порядок выдачи — из индекса, без сортировки
