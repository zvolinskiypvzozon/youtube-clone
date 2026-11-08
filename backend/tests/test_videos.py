"""Просмотр: карточка, ссылки на поток, лента, канал.

Логику политики покрывает test_policies.py; здесь — что каждый эндпоинт её применяет
(приватное, скрытое, не готовое — для всех зрителей), выдаёт рабочие ссылки и не кэшируется.
"""

import asyncio
import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.models import (
    UserRole,
    Video,
    VideoAccess,
    VideoFileKind,
)
from app.repositories import videos as videos_repo
from app.services import cursor, policies
from tests.helpers import (
    FAIL,
    HIDDEN,
    MEDIA,
    OK,
    PRIV,
    PROC,
    PUB,
    READY,
    UNL,
    api_user,
    auth_headers,
    channel_of,
    put_media,
    seed_video,
)

STATES = {
    "public": (PUB, READY, OK),
    "unlisted": (UNL, READY, OK),
    "private": (PRIV, READY, OK),
    "hidden": (PUB, READY, HIDDEN),
    "processing": (PUB, PROC, OK),
    "failed": (PUB, FAIL, OK),
}
ALL_STATES = frozenset(STATES)
# Кто какие видео может открыть (ADR 0006). У "granted" допуск ко всем видео владельца.
CAN_OPEN = {
    "anon": {"public", "unlisted"},
    "stranger": {"public", "unlisted"},
    "granted": {"public", "unlisted", "private"},
    "owner": ALL_STATES,
    "moderator": ALL_STATES,
    "admin": ALL_STATES,
}
VIEWERS = tuple(CAN_OPEN)
NOT_READY_STATES = {"processing", "failed"}


# --- Подготовка данных ----------------------------------------------------------------------


async def _viewers(client: AsyncClient, db: AsyncSession) -> dict[str, dict | None]:
    return {
        "anon": None,
        "stranger": await api_user(client, db),
        "granted": await api_user(client, db),
        "owner": await api_user(client, db),
        "moderator": await api_user(client, db, UserRole.moderator),
        "admin": await api_user(client, db, UserRole.admin),
    }


# --- Карточка и поток: доступ ---------------------------------------------------------------


@pytest.mark.parametrize("viewer_kind", VIEWERS)
async def test_card_and_stream_access_matrix(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    viewers = await _viewers(client, db)
    owner_channel = await channel_of(db, viewers["owner"])
    videos = {name: await seed_video(db, owner_channel, *state) for name, state in STATES.items()}
    granted_id = uuid.UUID(viewers["granted"]["id"])
    db.add_all(VideoAccess(video_id=v.id, user_id=granted_id) for v in videos.values())
    await db.commit()

    headers = auth_headers(viewers[viewer_kind])
    card_ok, stream_ok = set(), set()
    for name, video in videos.items():
        card = await client.get(f"/api/videos/{video.id}", headers=headers)
        assert card.status_code in (200, 404), (name, card.text)
        if card.status_code == 200:
            card_ok.add(name)

        st = await client.get(f"/api/videos/{video.id}/stream", headers=headers)
        if name in CAN_OPEN[viewer_kind] and name in NOT_READY_STATES:
            assert st.status_code == 409, (name, st.text)
        else:
            assert st.status_code in (200, 404), (name, st.text)
        if st.status_code == 200:
            stream_ok.add(name)

    assert card_ok == CAN_OPEN[viewer_kind]
    assert stream_ok == CAN_OPEN[viewer_kind] - NOT_READY_STATES


async def test_not_found_is_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    for suffix in ("", "/stream"):
        hidden = await client.get(f"/api/videos/{private.id}{suffix}", headers=stranger["auth"])
        missing = await client.get(f"/api/videos/{uuid.uuid4()}{suffix}", headers=stranger["auth"])
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()


async def test_card_state_only_for_owner_and_staff(client: AsyncClient, db: AsyncSession) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB, READY, HIDDEN)
    video.moderation_reason = "Нарушение правил"
    await db.commit()

    r = await client.get(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.json()["state"] == {
        "status": "ready",
        "visibility": "public",
        "moderation": "hidden",
        "moderation_reason": "Нарушение правил",
        "failure_reason": None,
    }
    r = await client.get(f"/api/videos/{video.id}", headers=moderator["auth"])
    assert r.json()["state"]["moderation"] == "hidden"

    await db.execute(update(Video).where(Video.id == video.id).values(moderation=OK))
    await db.commit()
    for headers in (stranger["auth"], {}):
        r = await client.get(f"/api/videos/{video.id}", headers=headers)
        assert r.status_code == 200
        assert r.json()["state"] is None


# --- Поток: ссылки ---------------------------------------------------------------------------


async def test_stream_urls_serve_files_with_range(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner))
    r = await client.get(f"/api/videos/{video.id}/stream")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_in"] == 15 * 60
    assert [s["quality"] for s in body["sources"]] == ["720p", "360p"]

    async with httpx.AsyncClient() as raw:
        kinds = (VideoFileKind.mp4_720, VideoFileKind.mp4_360)
        for source, kind in zip(body["sources"], kinds, strict=True):
            content = MEDIA[kind][0]
            ranged = await raw.get(source["url"], headers={"Range": "bytes=0-99"})
            assert ranged.status_code == 206
            assert ranged.content == content[:100]
            assert source["size_bytes"] == len(content)
        thumb = await raw.get(body["thumbnail_url"])
        assert thumb.status_code == 200
        assert thumb.headers["content-type"] == "image/jpeg"


async def test_stream_only_existing_qualities(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), media=False)
    await put_media(db, video, (VideoFileKind.mp4_360,))
    await db.commit()
    body = (await client.get(f"/api/videos/{video.id}/stream")).json()
    assert [s["quality"] for s in body["sources"]] == ["360p"]
    assert body["thumbnail_url"] is None


async def test_stream_ready_without_transcodes_is_conflict(
    client: AsyncClient, db: AsyncSession
) -> None:
    """ready без mp4 (нарушение данных) — 409, а не пустой список источников."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), media=False)
    await put_media(db, video, (VideoFileKind.thumbnail,))
    await db.commit()
    r = await client.get(f"/api/videos/{video.id}/stream")
    assert r.status_code == 409


async def test_stream_url_expires(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "stream_url_ttl_seconds", 1)
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner))
    url = (await client.get(f"/api/videos/{video.id}/stream")).json()["sources"][0]["url"]
    await asyncio.sleep(2.5)
    async with httpx.AsyncClient() as raw:
        assert (await raw.get(url)).status_code == 403


async def test_viewing_responses_are_not_cached(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner))
    for path in (
        f"/api/videos/{video.id}",
        f"/api/videos/{video.id}/stream",
        "/api/videos",
        f"/api/channels/{owner['username']}",
        f"/api/channels/{owner['username']}/videos",
    ):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-store", path


# --- Подпись ссылок (ADR 0004, дополнение 1) ------------------------------------------------


async def test_signing_does_not_depend_on_request_host(
    client: AsyncClient, db: AsyncSession, relative_urls: None
) -> None:
    """DoS из аудита: каждый новый Host создавал клиента подписи (~0,35 с CPU под общей
    блокировкой). Теперь Host запроса в подпись не входит — клиент один на процесс."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner))
    for i in range(40):
        r = await client.get(f"/api/videos/{video.id}/stream", headers={"Host": f"h{i}.example"})
        assert r.status_code == 200, r.text
        body = r.json()
        for url in [s["url"] for s in body["sources"]] + [body["thumbnail_url"]]:
            assert url.startswith(f"/{get_settings().s3_bucket_media}/videos/"), url
        feed = await client.get("/api/videos", headers={"Host": f"f{i}.example"})
        thumb = feed.json()["items"][0]["thumbnail_url"]
        assert thumb.startswith(f"/{get_settings().s3_bucket_media}/")
    assert storage._presign_client.cache_info().currsize == 1


async def test_relative_url_valid_only_via_signing_host(
    client: AsyncClient, db: AsyncSession, relative_urls: None
) -> None:
    """Так ходит nginx: тот же путь и подпись, Host = SIGNING_HOST. С другим Host — 403."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner))
    url = (await client.get(f"/api/videos/{video.id}/stream")).json()["sources"][0]["url"]
    async with httpx.AsyncClient(base_url="http://storage:9000") as raw:
        ok = await raw.get(url, headers={"Host": storage.SIGNING_HOST, "Range": "bytes=0-9"})
        assert ok.status_code == 206
        assert ok.content == MEDIA[VideoFileKind.mp4_720][0][:10]
        other = await raw.get(url, headers={"Host": "evil.example", "Range": "bytes=0-9"})
        assert other.status_code == 403


# --- Лента и канал -------------------------------------------------------------------------


async def _catalog_world(client: AsyncClient, db: AsyncSession) -> tuple[dict, dict, set]:
    """Два канала со всеми состояниями; возвращает (владелец1, владелец2, id публичных)."""
    a, b = await api_user(client, db), await api_user(client, db)
    listed = set()
    for owner in (a, b):
        channel = await channel_of(db, owner)
        for name, state in STATES.items():
            video = await seed_video(db, channel, *state, media=name == "public")
            if name == "public":
                listed.add(video.id)
    return a, b, listed


async def _all_pages(client: AsyncClient, path: str, headers: dict, limit: int) -> list[dict]:
    items, cur = [], None
    for _ in range(100):
        params = {"limit": limit} | ({"cursor": cur} if cur else {})
        r = await client.get(path, params=params, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) <= limit
        items += body["items"]
        cur = body["next_cursor"]
        if cur is None:
            return items
    raise AssertionError("пагинация не закончилась")


async def test_feed_shows_only_catalog_for_everyone(client: AsyncClient, db: AsyncSession) -> None:
    a, _, listed = await _catalog_world(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    for headers in ({}, a["auth"], moderator["auth"]):
        items = await _all_pages(client, "/api/videos", headers, limit=50)
        assert {uuid.UUID(i["id"]) for i in items} == listed
        assert all(i["thumbnail_url"] for i in items)


async def test_feed_pagination_with_equal_published_at(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    same = datetime.now(UTC) - timedelta(hours=1)
    videos = [await seed_video(db, channel, media=False, published_at=same) for _ in range(5)]
    videos += [
        await seed_video(db, channel, media=False, published_at=same + timedelta(minutes=i))
        for i in (1, 2)
    ]
    items = await _all_pages(client, "/api/videos", {}, limit=2)
    ids = [uuid.UUID(i["id"]) for i in items]
    assert len(ids) == len(set(ids)) == 7
    assert set(ids) == {v.id for v in videos}
    keys = [(datetime.fromisoformat(i["published_at"]), uuid.UUID(i["id"])) for i in items]
    assert keys == sorted(keys, reverse=True)
    assert all(i["thumbnail_url"] is None for i in items)


async def test_feed_rejects_bad_cursor_and_limit(client: AsyncClient) -> None:
    for bad in ("garbage", "!!!", cursor.encode(datetime.now(UTC), uuid.uuid4())[:-3], "Zm9v"):
        r = await client.get("/api/videos", params={"cursor": bad})
        assert r.status_code == 400, bad
        assert r.json()["detail"] == cursor.BAD_CURSOR
    naive = cursor.encode(datetime(2026, 1, 1), uuid.uuid4())  # без часового пояса
    assert (await client.get("/api/videos", params={"cursor": naive})).status_code == 400
    # Края диапазона: переполнение при переводе в UTC — 400, а не 500 из драйвера.
    for edge in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
        raw = base64.urlsafe_b64encode(f"{edge}|{uuid.uuid4()}".encode()).decode()
        r = await client.get("/api/videos", params={"cursor": raw})
        assert r.status_code == 400, (edge, r.text)
    for limit in (0, 51):
        assert (await client.get("/api/videos", params={"limit": limit})).status_code == 422


async def test_channel_card(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    r = await client.get(f"/api/channels/{owner['username'].upper()}")  # handle без учёта регистра
    assert r.status_code == 200
    assert r.json()["handle"] == owner["username"]
    assert (await client.get("/api/channels/no_such_channel")).status_code == 404
    assert (await client.get("/api/channels/a")).status_code == 422


async def test_channel_videos_only_catalog_of_that_channel(
    client: AsyncClient, db: AsyncSession
) -> None:
    a, b, listed = await _catalog_world(client, db)
    channel_a = await channel_of(db, a)
    expected = {
        v
        for v in listed
        if (await db.get(Video, v)).channel_id == channel_a.id  # type: ignore[union-attr]
    }
    assert len(expected) == 1
    # Владелец на своём канале видит тот же каталог: остальное — в «Моих видео».
    for headers in ({}, a["auth"], b["auth"]):
        items = await _all_pages(client, f"/api/channels/{a['username']}/videos", headers, 50)
        assert {uuid.UUID(i["id"]) for i in items} == expected
    r = await client.get("/api/channels/no_such_channel/videos")
    assert r.status_code == 404


# --- Индексы ------------------------------------------------------------------------------


async def _generic_plan(db: AsyncSession, stmt) -> str:
    """Generic plan запроса — так asyncpg выполняет prepared statement после нескольких
    вызовов. Seq scan выключен: на маленькой таблице он дешевле индекса, а проверяем, что
    индекс ПРИМЕНИМ к условию. PREPARE идёт простым протоколом (без параметров на проводе),
    а force_generic_plan не даёт planner-у подставить значения из EXECUTE."""
    conn = await db.connection()
    compiled = stmt.compile(dialect=conn.dialect)
    # SET LOCAL действует только внутри транзакции, а SQLAlchemy открывает её лениво —
    # на первом запросе через свой адаптер, не через прямые вызовы драйвера.
    await conn.execute(text("SELECT 1"))
    raw = (await conn.get_raw_connection()).driver_connection
    assert await raw.fetchval("SELECT now() = statement_timestamp()") is False, "нет транзакции"
    await raw.execute("SET LOCAL enable_seqscan = off")
    await raw.execute("SET LOCAL plan_cache_mode = force_generic_plan")
    await raw.execute(f"PREPARE plan_probe AS {compiled}")
    try:
        # Значения — из самого запроса, нетипизированными литералами (типы задал PREPARE).
        # NULL не годится: на NULL planner сворачивает условие в константу.
        values = [compiled.params[name] for name in compiled.positiontup or []]
        literals = ", ".join(f"'{v.isoformat() if isinstance(v, datetime) else v}'" for v in values)
        rows = await raw.fetch(f"EXPLAIN EXECUTE plan_probe({literals})")
    finally:
        await raw.execute("DEALLOCATE plan_probe")
    return "\n".join(r[0] for r in rows)


@pytest.mark.parametrize("with_cursor", [False, True])
async def test_feed_uses_partial_index(db: AsyncSession, with_cursor: bool) -> None:
    after = (datetime.now(UTC), uuid.uuid4()) if with_cursor else None
    stmt = videos_repo.catalog_page_stmt(policies.listed_filter(), after, 25)
    plan = await _generic_plan(db, stmt)
    assert "ix_videos_feed" in plan, plan


async def test_channel_videos_read_videos_by_index(db: AsyncSession) -> None:
    """Канал: годится и ix_videos_channel_published, и ix_videos_feed с фильтром по каналу —
    какой из них дешевле, решает статистика. Главное — videos не читается целиком."""
    stmt = videos_repo.catalog_page_stmt(
        policies.listed_filter(), (datetime.now(UTC), uuid.uuid4()), 25, channel_id=uuid.uuid4()
    )
    plan = await _generic_plan(db, stmt)
    assert "Seq Scan on videos" not in plan, plan
    assert "ix_videos_channel_published" in plan or "ix_videos_feed" in plan, plan
