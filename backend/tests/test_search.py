"""Поиск: GET /api/videos?q= — подстрока в названии без учёта регистра, новые сверху.

Только каталог (`listed_filter`, K3, K4): готовые публичные не скрытые, одинаково для любого
зрителя, включая владельца и staff. `%`, `_` и `\\` во вводе — буквальные символы.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Video
from app.repositories import videos as videos_repo
from app.services import policies
from tests.helpers import (
    MANAGE_STATES,
    MANAGE_VIEWERS,
    PUB,
    api_user,
    auth_headers,
    channel_of,
    generic_plan,
    manage_matrix,
    seed_video,
)


async def _video(db: AsyncSession, channel: Channel, title: str, minutes_ago: int = 0) -> Video:
    """Готовое публичное видео с названием и временем публикации (без файлов — быстрее)."""
    video = await seed_video(
        db,
        channel,
        PUB,
        media=False,
        published_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    await db.execute(update(Video).where(Video.id == video.id).values(title=title))
    await db.commit()
    return video


async def _search(client: AsyncClient, q: str, headers: dict | None = None, **params) -> list:
    r = await client.get("/api/videos", params={"q": q, **params}, headers=headers or {})
    assert r.status_code == 200, r.text
    return [item["title"] for item in r.json()["items"]]


async def test_substring_case_insensitive(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    await _video(db, channel, "Как испечь ХЛЕБ дома", minutes_ago=2)
    await _video(db, channel, "Хлебопечка: обзор", minutes_ago=1)
    await _video(db, channel, "Рецепт пиццы")

    assert await _search(client, "хлеб") == ["Хлебопечка: обзор", "Как испечь ХЛЕБ дома"]
    assert await _search(client, "  ПЕЧ ") == ["Хлебопечка: обзор", "Как испечь ХЛЕБ дома"]
    assert await _search(client, "суши") == []
    # Только название: слово из описания не находится.
    pizza = await db.get(Video, (await _video(db, channel, "Без слова")).id)
    pizza.description = "тут есть суши"
    await db.commit()
    assert await _search(client, "суши") == []
    # Без q — обычная лента, все четыре.
    assert len((await client.get("/api/videos")).json()["items"]) == 4


@pytest.mark.parametrize("viewer_kind", [*MANAGE_VIEWERS, "owner"])
async def test_search_access_matrix(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    """K3, K4: в поиске только готовое публичное не скрытое — для любого зрителя, включая
    владельца, допущенного и staff (остальное они видят в «Моих видео» и модерации)."""
    owner = await api_user(client, db)
    if viewer_kind == "owner":
        videos, _ = await manage_matrix(client, db, owner, "anon")
        viewer: dict | None = owner
    else:
        videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
    for name, video in videos.items():
        await db.execute(update(Video).where(Video.id == video.id).values(title=f"иголка {name}"))
    await db.commit()
    assert set(videos) == set(MANAGE_STATES)

    found = await _search(client, "иголка", auth_headers(viewer))
    assert found == ["иголка public"]


@pytest.mark.parametrize(
    ("q", "expected"),
    [("%", ["100% хлеб"]), ("_", ["snake_case"]), ("\\", ["back\\slash"]), ("0%", ["100% хлеб"])],
)
async def test_like_wildcards_are_literal(
    client: AsyncClient, db: AsyncSession, q: str, expected: list[str]
) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    for title in ("100% хлеб", "snake_case", "back\\slash", "обычное видео"):
        await _video(db, channel, title)
    assert await _search(client, q) == expected


async def test_pagination(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    for i in range(5):
        await _video(db, channel, f"серия {i}", minutes_ago=10 - i)
    await _video(db, channel, "другое")

    seen, cursor = [], None
    while True:
        params = {"q": "серия", "limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/api/videos", params=params)).json()
        seen += [item["title"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"серия {i}" for i in range(4, -1, -1)]  # новые сверху
    r = await client.get("/api/videos", params={"q": "серия", "cursor": "мусор"})
    assert r.status_code == 400


@pytest.mark.parametrize(
    "q", ["", "   ", "я" * 101, "хлеб\x00"], ids=["empty", "spaces", "too-long", "nul"]
)
async def test_invalid_query_is_422(client: AsyncClient, q: str) -> None:
    r = await client.get("/api/videos", params={"q": q})
    assert r.status_code == 422, r.text


async def test_query_length_limit_ok(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    await _video(db, await channel_of(db, owner), "я" * 100)
    assert await _search(client, "я" * 100) == ["я" * 100]


async def test_search_does_not_read_whole_table(db: AsyncSession) -> None:
    """Поиск читает videos по индексу (триграммный или ленты — решает статистика), а не
    целиком."""
    stmt = videos_repo.catalog_page_stmt(
        policies.listed_filter(),
        (datetime.now(UTC), uuid.uuid4()),
        25,
        title_contains="хлеб",
    )
    plan = await generic_plan(db, stmt)
    assert "Seq Scan on videos" not in plan, plan
