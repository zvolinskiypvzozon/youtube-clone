"""Лента подписок и «Понравившиеся» (день 9): только то, что зритель вправе видеть."""

import uuid

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserRole
from tests.helpers import HIDDEN, PRIV, PROC, UNL, api_user, channel_of, seed_video


async def _ids(client: AsyncClient, path: str, user: dict, **params) -> list[str]:
    r = await client.get(path, headers=user["auth"], params=params)
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json()["items"]]


async def _ban(db: AsyncSession, user: dict) -> None:
    await db.execute(
        update(User)
        .where(User.id == uuid.UUID(user["id"]))
        .values(is_banned=True, banned_reason="тест", banned_by_role=UserRole.admin)
    )
    await db.commit()


async def test_feed_only_subscribed_catalog(client: AsyncClient, db: AsyncSession) -> None:
    viewer = await api_user(client, db)
    author = await api_user(client, db)
    stranger = await api_user(client, db)
    banned = await api_user(client, db)
    ch = await channel_of(db, author)
    public = await seed_video(db, ch)
    hidden_ids = [
        (await seed_video(db, ch, PRIV)).id,
        (await seed_video(db, ch, UNL)).id,
        (await seed_video(db, ch, moderation=HIDDEN)).id,
        (await seed_video(db, ch, status=PROC)).id,
    ]
    foreign = await seed_video(db, await channel_of(db, stranger))
    banned_video = await seed_video(db, await channel_of(db, banned))
    for who in (author, banned):
        r = await client.put(
            f"/api/channels/{who['username']}/subscription", headers=viewer["auth"]
        )
        assert r.status_code == 200
    await _ban(db, banned)

    ids = await _ids(client, "/api/me/feed", viewer)
    assert ids == [str(public.id)]
    assert str(foreign.id) not in ids and str(banned_video.id) not in ids
    assert not set(map(str, hidden_ids)) & set(ids)
    # Отписка — видео канала уходит из ленты.
    await client.delete(f"/api/channels/{author['username']}/subscription", headers=viewer["auth"])
    assert await _ids(client, "/api/me/feed", viewer) == []


async def test_feed_pagination_newest_first(client: AsyncClient, db: AsyncSession) -> None:
    viewer = await api_user(client, db)
    author = await api_user(client, db)
    ch = await channel_of(db, author)
    videos = [await seed_video(db, ch, media=False) for _ in range(3)]
    await client.put(f"/api/channels/{author['username']}/subscription", headers=viewer["auth"])
    r = await client.get("/api/me/feed", headers=viewer["auth"], params={"limit": 2})
    page = r.json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = await _ids(client, "/api/me/feed", viewer, limit=2, cursor=page["next_cursor"])
    got = [v["id"] for v in page["items"]] + rest
    expected = [
        str(v.id) for v in sorted(videos, key=lambda v: (v.published_at, v.id), reverse=True)
    ]
    assert got == expected


async def test_liked_only_visible_likes_newest_first(client: AsyncClient, db: AsyncSession) -> None:
    viewer = await api_user(client, db)
    author = await api_user(client, db)
    banned = await api_user(client, db)
    ch = await channel_of(db, author)
    first = await seed_video(db, ch)
    second = await seed_video(db, ch)
    disliked = await seed_video(db, ch)
    became_private = await seed_video(db, ch)
    became_hidden = await seed_video(db, ch)
    banned_video = await seed_video(db, await channel_of(db, banned))
    for video in (first, second, became_private, became_hidden, banned_video):
        r = await client.put(
            f"/api/videos/{video.id}/reaction", json={"kind": "like"}, headers=viewer["auth"]
        )
        assert r.status_code == 200, r.text
    await client.put(
        f"/api/videos/{disliked.id}/reaction", json={"kind": "dislike"}, headers=viewer["auth"]
    )
    # Стали недоступны зрителю: приватное без допуска, скрытое модерацией, автор забанен.
    r = await client.patch(
        f"/api/videos/{became_private.id}", json={"visibility": "private"}, headers=author["auth"]
    )
    assert r.status_code == 200
    became_hidden.moderation = HIDDEN
    became_hidden.moderation_reason = "нарушение"
    await db.commit()
    await _ban(db, banned)

    ids = await _ids(client, "/api/me/liked", viewer)
    assert ids == [str(second.id), str(first.id)]  # новые лайки сверху
    # Снятый лайк — видео уходит.
    await client.delete(f"/api/videos/{second.id}/reaction", headers=viewer["auth"])
    assert await _ids(client, "/api/me/liked", viewer) == [str(first.id)]


async def test_liked_pagination_by_like_time(client: AsyncClient, db: AsyncSession) -> None:
    viewer = await api_user(client, db)
    ch = await channel_of(db, await api_user(client, db))
    videos = [await seed_video(db, ch, media=False) for _ in range(3)]
    for video in videos:
        await client.put(
            f"/api/videos/{video.id}/reaction", json={"kind": "like"}, headers=viewer["auth"]
        )
    r = await client.get("/api/me/liked", headers=viewer["auth"], params={"limit": 2})
    page = r.json()
    rest = await _ids(client, "/api/me/liked", viewer, limit=2, cursor=page["next_cursor"])
    assert [v["id"] for v in page["items"]] + rest == [str(v.id) for v in reversed(videos)]


async def test_anonymous_is_401(client: AsyncClient) -> None:
    for path in ("/api/me/feed", "/api/me/liked"):
        assert (await client.get(path)).status_code == 401


async def test_liked_keeps_private_with_access_and_own(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Приватное с допуском и своё приватное — доступны, значит остаются в «Понравившихся»."""
    viewer = await api_user(client, db)
    author = await api_user(client, db)
    granted = await seed_video(db, await channel_of(db, author))
    own = await seed_video(db, await channel_of(db, viewer))
    for video in (granted, own):
        await client.put(
            f"/api/videos/{video.id}/reaction", json={"kind": "like"}, headers=viewer["auth"]
        )
    r = await client.post(
        f"/api/videos/{granted.id}/access",
        json={"username": viewer["username"]},
        headers=author["auth"],
    )
    assert r.status_code == 201, r.text
    for video, owner in ((granted, author), (own, viewer)):
        r = await client.patch(
            f"/api/videos/{video.id}", json={"visibility": "private"}, headers=owner["auth"]
        )
        assert r.status_code == 200
    assert set(await _ids(client, "/api/me/liked", viewer)) == {str(granted.id), str(own.id)}


async def test_liked_pagination_same_like_time_tie_break(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Одинаковое время лайка: тай-брейк по id, страницы без дублей и пропусков."""
    from datetime import UTC, datetime

    from app.models import Reaction

    viewer = await api_user(client, db)
    ch = await channel_of(db, await api_user(client, db))
    videos = [await seed_video(db, ch, media=False) for _ in range(3)]
    for video in videos:
        await client.put(
            f"/api/videos/{video.id}/reaction", json={"kind": "like"}, headers=viewer["auth"]
        )
    same = datetime(2026, 11, 1, tzinfo=UTC)
    await db.execute(
        update(Reaction).where(Reaction.user_id == uuid.UUID(viewer["id"])).values(created_at=same)
    )
    await db.commit()
    got, cursor = [], None
    for _ in range(4):
        params = {"limit": 1} | ({"cursor": cursor} if cursor else {})
        page = (await client.get("/api/me/liked", headers=viewer["auth"], params=params)).json()
        got += [v["id"] for v in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert got == sorted((str(v.id) for v in videos), reverse=True)
