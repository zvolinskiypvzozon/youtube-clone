"""Плейлисты (ADR 0006 доп. 7, K4).

- кто открывает плейлист: public/unlisted — все, private — только владелец (staff — нет);
- состав, video_count и превью — только видео, которые может открыть ТЕКУЩИЙ зритель
  (матрица 6 зрителей × 24 состояния видео из test_policies);
- управление — только владелец: не может открыть → 404, может → 403, аноним → 401;
- в плейлист — только свои видео; лимиты; порядок блокировок (тесты гонок).
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Playlist,
    PlaylistItem,
    PlaylistVisibility,
    User,
    UserRole,
    Video,
    VideoAccess,
    VideoStatus,
)
from app.schemas.playlists import PlaylistCreateIn
from app.services import cursor
from app.services import manage as manage_service
from app.services import playlists as playlists_service
from tests.helpers import (
    HIDDEN,
    OK,
    PRIV,
    PUB,
    READY,
    UNL,
    api_user,
    auth_headers,
    channel_of,
    generic_plan,
    seed_video,
    waits_for,
)
from tests.test_policies import ALL, EXPECTED_OWNER_CHANNEL, VIEWERS, Combo

PL_PUBLIC, PL_UNLISTED, PL_PRIVATE = "public", "unlisted", "private"
PL_VISIBILITIES = (PL_PUBLIC, PL_UNLISTED, PL_PRIVATE)
ROLES = {"moderator": UserRole.moderator, "admin": UserRole.admin}


async def _create(
    client: AsyncClient, user: dict, visibility: str = PL_PUBLIC, title: str = "Плейлист"
) -> dict:
    r = await client.post(
        "/api/playlists", json={"title": title, "visibility": visibility}, headers=user["auth"]
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _add(client: AsyncClient, user: dict | None, pid: str, vid: uuid.UUID) -> Response:
    return await client.put(f"/api/playlists/{pid}/videos/{vid}", headers=auth_headers(user))


async def _ids(client: AsyncClient, viewer: dict | None, pid: str, limit: int = 50) -> list[str]:
    """Весь состав, видимый зрителю, по страницам."""
    ids, cur = [], None
    while True:
        params = {"limit": limit, **({"cursor": cur} if cur else {})}
        r = await client.get(
            f"/api/playlists/{pid}/videos", params=params, headers=auth_headers(viewer)
        )
        assert r.status_code == 200, r.text
        ids += [i["id"] for i in r.json()["items"]]
        cur = r.json()["next_cursor"]
        if cur is None:
            return ids


async def _viewer(client: AsyncClient, db: AsyncSession, kind: str) -> dict | None:
    if kind == "anon":
        return None
    return await api_user(client, db, ROLES.get(kind, UserRole.user))


async def _items(db: AsyncSession, pid: str) -> list[PlaylistItem]:
    stmt = (
        select(PlaylistItem)
        .where(PlaylistItem.playlist_id == uuid.UUID(pid))
        .order_by(PlaylistItem.position)
        .execution_options(populate_existing=True)
    )
    await db.rollback()  # новый снимок: изменения из запросов API видны
    return list(await db.scalars(stmt))


async def _me(session: AsyncSession, user: dict) -> User:
    return await session.get(User, uuid.UUID(user["id"]))


# --- Создание и валидация -------------------------------------------------------------------


async def test_create_and_get(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    r = await client.post(
        "/api/playlists",
        json={"title": "  Лучшее  ", "description": "описание", "visibility": "unlisted"},
        headers=owner["auth"],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "Лучшее"  # обрезка пробелов
    assert body["visibility"] == "unlisted"
    assert body["can_manage"] is True
    assert body["video_count"] == 0
    assert body["thumbnail_url"] is None
    assert body["channel"]["handle"] == owner["username"]
    assert r.headers["cache-control"] == "no-store"

    other = await api_user(client, db)
    r = await client.get(f"/api/playlists/{body['id']}", headers=other["auth"])
    assert r.status_code == 200
    assert r.json()["visibility"] is None  # видимость — только владельцу
    assert r.json()["can_manage"] is False


async def test_create_requires_login(client: AsyncClient) -> None:
    r = await client.post("/api/playlists", json={"title": "x", "visibility": "public"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "visibility": "public"},
        {"title": "   ", "visibility": "public"},
        {"title": "a\x00b", "visibility": "public"},
        {"title": "x" * 151, "visibility": "public"},
        {"title": "x", "description": "d" * 5001, "visibility": "public"},
        {"title": "x", "description": "a\x00", "visibility": "public"},
        {"title": "x"},
        {"title": "x", "visibility": "secret"},
        {"title": "x", "visibility": "public", "extra": 1},
    ],
)
async def test_create_invalid_is_422(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    owner = await api_user(client, db)
    r = await client.post("/api/playlists", json=payload, headers=owner["auth"])
    assert r.status_code == 422, r.text


async def test_title_150_and_description_5000_accepted(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    r = await client.post(
        "/api/playlists",
        json={"title": "я" * 150, "description": "д" * 5000, "visibility": "private"},
        headers=owner["auth"],
    )
    assert r.status_code == 201, r.text


@pytest.mark.parametrize(
    "payload", [{}, {"title": None}, {"visibility": None}, {"title": " "}, {"x": 1}]
)
async def test_update_invalid_is_422(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    r = await client.patch(f"/api/playlists/{pl['id']}", json=payload, headers=owner["auth"])
    assert r.status_code == 422, r.text


async def test_update(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner, PL_PUBLIC)
    r = await client.patch(
        f"/api/playlists/{pl['id']}",
        json={"title": "Новое", "visibility": "private"},
        headers=owner["auth"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Новое"
    assert r.json()["visibility"] == "private"
    assert r.json()["description"] == ""
    # Стал приватным — постороннему 404.
    other = await api_user(client, db)
    r = await client.get(f"/api/playlists/{pl['id']}", headers=other["auth"])
    assert r.status_code == 404


async def test_playlists_limit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_service, "PLAYLISTS_LIMIT", 2)
    owner = await api_user(client, db)
    await _create(client, owner)
    await _create(client, owner)
    r = await client.post(
        "/api/playlists", json={"title": "x", "visibility": "public"}, headers=owner["auth"]
    )
    assert r.status_code == 409
    assert r.json()["detail"] == playlists_service.playlists_limit_reached()
    # Лимит — на канал: у другого пользователя свой.
    await _create(client, await api_user(client, db))


# --- Кто открывает плейлист -----------------------------------------------------------------

PL_VIEWERS = ("anon", "stranger", "owner", "moderator", "admin")


def _opens(viewer_kind: str, visibility: str) -> bool:
    """Приватный плейлист — только владельцу; staff тоже нет (решение 2026-11-10)."""
    return visibility != PL_PRIVATE or viewer_kind == "owner"


@pytest.mark.parametrize("visibility", PL_VISIBILITIES)
@pytest.mark.parametrize("viewer_kind", PL_VIEWERS)
async def test_view_matrix(
    client: AsyncClient, db: AsyncSession, viewer_kind: str, visibility: str
) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner, visibility)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    assert (await _add(client, owner, pl["id"], video.id)).status_code == 201
    viewer = owner if viewer_kind == "owner" else await _viewer(client, db, viewer_kind)

    expected = 200 if _opens(viewer_kind, visibility) else 404
    card = await client.get(f"/api/playlists/{pl['id']}", headers=auth_headers(viewer))
    items = await client.get(f"/api/playlists/{pl['id']}/videos", headers=auth_headers(viewer))
    assert card.status_code == expected, card.text
    assert items.status_code == expected, items.text
    if expected == 404:
        # Ответ неотличим от несуществующего плейлиста.
        missing = await client.get(f"/api/playlists/{uuid.uuid4()}", headers=auth_headers(viewer))
        assert card.json() == missing.json()


MANAGE_CALLS = ("patch", "delete", "put_item", "delete_item")


async def _manage(
    client: AsyncClient, call: str, viewer: dict | None, pid: str, vid: uuid.UUID
) -> Response:
    h = auth_headers(viewer)
    if call == "patch":
        return await client.patch(f"/api/playlists/{pid}", json={"title": "взлом"}, headers=h)
    if call == "delete":
        return await client.delete(f"/api/playlists/{pid}", headers=h)
    if call == "put_item":
        return await client.put(f"/api/playlists/{pid}/videos/{vid}", headers=h)
    return await client.delete(f"/api/playlists/{pid}/videos/{vid}", headers=h)


@pytest.mark.parametrize("call", MANAGE_CALLS)
@pytest.mark.parametrize("visibility", PL_VISIBILITIES)
@pytest.mark.parametrize("viewer_kind", ["anon", "stranger", "moderator", "admin"])
async def test_manage_matrix(
    client: AsyncClient, db: AsyncSession, viewer_kind: str, visibility: str, call: str
) -> None:
    """Чужой плейлист: аноним → 401, не может открыть → 404, может → 403. Ничего не меняется.
    Видео для put_item — своё у зрителя (иначе отказ мог бы прийти из проверки видео)."""
    owner = await api_user(client, db)
    pl = await _create(client, owner, visibility)
    own_video = await seed_video(db, await channel_of(db, owner), PUB)
    assert (await _add(client, owner, pl["id"], own_video.id)).status_code == 201
    viewer = await _viewer(client, db, viewer_kind)
    vid = (
        own_video.id
        if viewer is None or call == "delete_item"
        else (await seed_video(db, await channel_of(db, viewer), PUB)).id
    )

    r = await _manage(client, call, viewer, pl["id"], vid)
    expected = 401 if viewer is None else 403 if _opens(viewer_kind, visibility) else 404
    assert r.status_code == expected, r.text

    owner_view = await client.get(f"/api/playlists/{pl['id']}", headers=owner["auth"])
    assert owner_view.status_code == 200
    assert owner_view.json()["title"] == "Плейлист"
    assert [i.video_id for i in await _items(db, pl["id"])] == [own_video.id]


@pytest.mark.parametrize("call", MANAGE_CALLS)
async def test_manage_missing_playlist_404(
    client: AsyncClient, db: AsyncSession, call: str
) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await _manage(client, call, owner, str(uuid.uuid4()), video.id)
    assert r.status_code == 404, r.text


async def test_delete_playlist(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _add(client, owner, pl["id"], video.id)
    r = await client.delete(f"/api/playlists/{pl['id']}", headers=owner["auth"])
    assert r.status_code == 204
    assert (await client.get(f"/api/playlists/{pl['id']}")).status_code == 404
    assert await _items(db, pl["id"]) == []
    assert await db.get(Video, video.id) is not None  # видео не затронуто


# --- Состав: K4 -----------------------------------------------------------------------------

# Порядок в плейлисте: первыми — невидимые постороннему готовые видео с превью. Если превью
# или состав считаются без фильтра зрителя, первое превью — от приватного видео.
_FIRST = [(PRIV, READY, OK), (PUB, READY, HIDDEN)]
ORDER: list[Combo] = _FIRST + [c for c in sorted(ALL) if c not in _FIRST]


@pytest.mark.parametrize("viewer_kind", VIEWERS)
async def test_items_matrix(client: AsyncClient, db: AsyncSession, viewer_kind: str) -> None:
    """Публичный плейлист со всеми 24 состояниями видео владельца: зритель видит ровно
    то, что может открыть (`visible_filter` текущего зрителя, а не владельца плейлиста);
    video_count и превью — по тем же видео."""
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner, PL_PUBLIC)
    videos: dict[Combo, Video] = {}
    for combo in ORDER:
        videos[combo] = await seed_video(db, channel, *combo)
        r = await _add(client, owner, pl["id"], videos[combo].id)
        assert r.status_code == 201, r.text  # своё видео — в любом состоянии

    viewer = owner if viewer_kind == "owner" else await _viewer(client, db, viewer_kind)
    if viewer_kind == "granted":
        db.add_all(
            VideoAccess(video_id=v.id, user_id=uuid.UUID(viewer["id"])) for v in videos.values()
        )
        await db.commit()

    expected = EXPECTED_OWNER_CHANNEL[viewer_kind]
    expected_ids = [str(videos[c].id) for c in ORDER if c in expected]
    assert await _ids(client, viewer, pl["id"], limit=5) == expected_ids  # с пагинацией

    card = (await client.get(f"/api/playlists/{pl['id']}", headers=auth_headers(viewer))).json()
    assert card["video_count"] == len(expected_ids)
    first = next(c for c in ORDER if c in expected)
    if first[1] == READY:
        assert str(videos[first].id) in card["thumbnail_url"]
    else:
        assert card["thumbnail_url"] is None

    # То же в списке канала (плейлист публичный).
    r = await client.get(
        f"/api/channels/{owner['username']}/playlists", headers=auth_headers(viewer)
    )
    [item] = r.json()["items"]
    assert item["video_count"] == len(expected_ids)
    if first[1] == READY:
        assert str(videos[first].id) in item["thumbnail_url"]
    else:
        assert item["thumbnail_url"] is None

    # state — только владельцу видео и staff.
    page = (
        await client.get(f"/api/playlists/{pl['id']}/videos", headers=auth_headers(viewer))
    ).json()
    sees_state = viewer_kind in ("owner", "moderator", "admin")
    assert all((i["state"] is not None) is sees_state for i in page["items"])


async def test_revoked_access_removes_video(client: AsyncClient, db: AsyncSession) -> None:
    owner, granted = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner, PL_PUBLIC)
    private = await seed_video(db, channel, PRIV)
    public = await seed_video(db, channel, PUB)
    for v in (private, public):
        await _add(client, owner, pl["id"], v.id)
    r = await client.post(
        f"/api/videos/{private.id}/access",
        json={"username": granted["username"]},
        headers=owner["auth"],
    )
    assert r.status_code == 201
    assert await _ids(client, granted, pl["id"]) == [str(private.id), str(public.id)]

    r = await client.delete(
        f"/api/videos/{private.id}/access/{granted['id']}", headers=owner["auth"]
    )
    assert r.status_code == 204
    assert await _ids(client, granted, pl["id"]) == [str(public.id)]
    card = await client.get(f"/api/playlists/{pl['id']}", headers=granted["auth"])
    assert card.json()["video_count"] == 1


@pytest.mark.parametrize("change", ["private", "hidden"])
async def test_video_changed_after_adding(
    client: AsyncClient, db: AsyncSession, change: str
) -> None:
    """Видео стало приватным (владелец) или скрытым (модератор) после добавления: у других
    пропадает, владелец видит со state."""
    owner, stranger = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    pl = await _create(client, owner, PL_PUBLIC)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _add(client, owner, pl["id"], video.id)
    assert await _ids(client, stranger, pl["id"]) == [str(video.id)]

    if change == "private":
        r = await client.patch(
            f"/api/videos/{video.id}", json={"visibility": "private"}, headers=owner["auth"]
        )
    else:
        r = await client.put(
            f"/api/admin/videos/{video.id}/moderation",
            json={"state": "hidden", "reason": "нарушение"},
            headers=moderator["auth"],
        )
    assert r.status_code == 200, r.text

    for viewer in (None, stranger):
        assert await _ids(client, viewer, pl["id"]) == []
        card = await client.get(f"/api/playlists/{pl['id']}", headers=auth_headers(viewer))
        assert card.json()["video_count"] == 0
        assert card.json()["thumbnail_url"] is None
    page = (await client.get(f"/api/playlists/{pl['id']}/videos", headers=owner["auth"])).json()
    [item] = page["items"]
    assert item["id"] == str(video.id)
    state = item["state"]
    assert (state["visibility"], state["moderation"]) == (
        ("private", "ok") if change == "private" else ("public", "hidden")
    )


# --- Добавление и удаление элементов -------------------------------------------------------


async def test_add_repeat_and_order(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner)
    v1, v2, v3, v4 = [await seed_video(db, channel, PUB) for _ in range(4)]
    for v in (v1, v2, v3):
        r = await _add(client, owner, pl["id"], v.id)
        assert r.status_code == 201, r.text
    assert r.json()["position"] == 3

    again = await _add(client, owner, pl["id"], v1.id)
    assert again.status_code == 200
    assert again.json()["position"] == 1  # повтор не двигает видео

    r = await client.delete(f"/api/playlists/{pl['id']}/videos/{v2.id}", headers=owner["auth"])
    assert r.status_code == 204
    r = await client.delete(f"/api/playlists/{pl['id']}/videos/{v2.id}", headers=owner["auth"])
    assert r.status_code == 204  # идемпотентно
    r = await client.delete(
        f"/api/playlists/{pl['id']}/videos/{uuid.uuid4()}", headers=owner["auth"]
    )
    assert r.status_code == 204

    assert (await _add(client, owner, pl["id"], v4.id)).json()["position"] == 4
    assert [i.position for i in await _items(db, pl["id"])] == [1, 3, 4]
    assert await _ids(client, None, pl["id"], limit=1) == [str(v1.id), str(v3.id), str(v4.id)]


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def test_add_and_remove_touch_updated_at(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _add(client, owner, pl["id"], video.id)
    after_add = (await client.get(f"/api/playlists/{pl['id']}")).json()["updated_at"]
    assert _ts(after_add) > _ts(pl["updated_at"])
    r = await client.delete(f"/api/playlists/{pl['id']}/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204
    after_remove = (await client.get(f"/api/playlists/{pl['id']}")).json()["updated_at"]
    assert _ts(after_remove) > _ts(after_add)


async def test_update_description_only_and_make_public(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner, PL_PRIVATE, title="Скрытый")
    channel_url = f"/api/channels/{owner['username']}/playlists"
    assert (await client.get(channel_url)).json()["items"] == []

    r = await client.patch(
        f"/api/playlists/{pl['id']}", json={"description": "новое"}, headers=owner["auth"]
    )
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["description"], r.json()["visibility"]) == (
        "Скрытый",
        "новое",
        "private",
    )
    r = await client.patch(
        f"/api/playlists/{pl['id']}", json={"visibility": "public"}, headers=owner["auth"]
    )
    assert r.status_code == 200
    assert [i["id"] for i in (await client.get(channel_url)).json()["items"]] == [pl["id"]]
    assert (await client.get(f"/api/playlists/{pl['id']}")).status_code == 200


@pytest.mark.parametrize("payload", [{"title": "a\x00"}, {"description": "a\x00"}])
async def test_update_nul_is_422(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    r = await client.patch(f"/api/playlists/{pl['id']}", json=payload, headers=owner["auth"])
    assert r.status_code == 422, r.text


async def test_my_playlists_pagination(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    created = [await _create(client, owner, v) for v in (*PL_VISIBILITIES, PL_PUBLIC)]
    seen, cur = [], None
    while True:
        params = {"limit": 3, **({"cursor": cur} if cur else {})}
        r = await client.get("/api/me/playlists", params=params, headers=owner["auth"])
        assert r.status_code == 200, r.text
        seen += [i["id"] for i in r.json()["items"]]
        cur = r.json()["next_cursor"]
        if cur is None:
            break
    assert seen == [p["id"] for p in reversed(created)]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("public", 403),
        ("unlisted", 403),
        ("private", 404),
        ("hidden", 404),
        ("granted_private", 403),
        ("missing", 404),
    ],
)
async def test_add_foreign_video(
    client: AsyncClient, db: AsyncSession, state: str, expected: int
) -> None:
    """Только своё видео: чужое, которое зритель может открыть, → 403, не может → 404."""
    owner, other = await api_user(client, db), await api_user(client, db)
    pl = await _create(client, owner)
    states = {
        "public": (PUB, READY, OK),
        "unlisted": (UNL, READY, OK),
        "private": (PRIV, READY, OK),
        "hidden": (PUB, READY, HIDDEN),
        "granted_private": (PRIV, READY, OK),
    }
    if state == "missing":
        vid = uuid.uuid4()
    else:
        video = await seed_video(db, await channel_of(db, other), *states[state])
        vid = video.id
        if state == "granted_private":
            db.add(VideoAccess(video_id=vid, user_id=uuid.UUID(owner["id"])))
            await db.commit()
    r = await _add(client, owner, pl["id"], vid)
    assert r.status_code == expected, r.text
    assert await _items(db, pl["id"]) == []


@pytest.mark.parametrize("role", [UserRole.moderator, UserRole.admin])
async def test_staff_cannot_add_foreign_video(
    client: AsyncClient, db: AsyncSession, role: UserRole
) -> None:
    """Staff видит любое видео, но добавить в свой плейлист может только своё."""
    staff, other = await api_user(client, db, role), await api_user(client, db)
    pl = await _create(client, staff)
    video = await seed_video(db, await channel_of(db, other), PRIV)
    r = await _add(client, staff, pl["id"], video.id)
    assert r.status_code == 403
    assert await _items(db, pl["id"]) == []


async def test_items_limit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_service, "ITEMS_LIMIT", 2)
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner)
    v1, v2, v3 = [await seed_video(db, channel, PUB) for _ in range(3)]
    for v in (v1, v2):
        assert (await _add(client, owner, pl["id"], v.id)).status_code == 201
    r = await _add(client, owner, pl["id"], v3.id)
    assert r.status_code == 409
    assert r.json()["detail"] == playlists_service.items_limit_reached()
    # Повтор уже добавленного на лимите — 200, а не 409.
    assert (await _add(client, owner, pl["id"], v1.id)).status_code == 200


async def test_deleting_video_removes_item(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner)
    v1, v2 = await seed_video(db, channel, PUB), await seed_video(db, channel, PUB)
    for v in (v1, v2):
        await _add(client, owner, pl["id"], v.id)
    r = await client.delete(f"/api/videos/{v1.id}", headers=owner["auth"])
    assert r.status_code == 204
    assert [i.video_id for i in await _items(db, pl["id"])] == [v2.id]
    assert (await client.get(f"/api/playlists/{pl['id']}")).json()["video_count"] == 1


# --- Списки ---------------------------------------------------------------------------------


@pytest.mark.parametrize("viewer_kind", PL_VIEWERS)
async def test_channel_lists_only_public(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    """Страница канала — только public, для любого зрителя, включая владельца и staff."""
    owner = await api_user(client, db)
    created = {v: await _create(client, owner, v, title=v) for v in PL_VISIBILITIES}
    viewer = owner if viewer_kind == "owner" else await _viewer(client, db, viewer_kind)
    r = await client.get(
        f"/api/channels/{owner['username']}/playlists", headers=auth_headers(viewer)
    )
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["items"]] == [created[PL_PUBLIC]["id"]]
    assert r.json()["items"][0]["visibility"] is None


async def test_channel_list_pagination_and_errors(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    created = [await _create(client, owner, title=f"p{i}") for i in range(5)]
    seen, cur = [], None
    while True:
        params = {"limit": 2, **({"cursor": cur} if cur else {})}
        r = await client.get(f"/api/channels/{owner['username']}/playlists", params=params)
        assert r.status_code == 200, r.text
        seen += [i["id"] for i in r.json()["items"]]
        cur = r.json()["next_cursor"]
        if cur is None:
            break
    assert seen == [p["id"] for p in reversed(created)]  # новые сверху, без пропусков и дублей

    r = await client.get("/api/channels/no_such_channel/playlists")
    assert r.status_code == 404
    r = await client.get(f"/api/channels/{owner['username']}/playlists", params={"cursor": "xx"})
    assert r.status_code == 400
    assert r.json()["detail"] == cursor.BAD_CURSOR
    r = await client.get("/api/me/playlists", params={"cursor": "xx"}, headers=owner["auth"])
    assert r.status_code == 400
    assert r.json()["detail"] == cursor.BAD_CURSOR


@pytest.mark.parametrize(
    "bad",
    [
        "xx",
        cursor.encode_position(-1),
        cursor.encode_position(2**31),  # за пределами integer — 400, а не 500 из asyncpg
        cursor.encode(datetime(2026, 1, 1, tzinfo=UTC), uuid.uuid4()),  # курсор другого списка
    ],
)
async def test_items_bad_cursor_is_400(client: AsyncClient, db: AsyncSession, bad: str) -> None:
    """Как у ленты, поиска и комментариев: 400 с общим текстом cursor.BAD_CURSOR."""
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    r = await client.get(f"/api/playlists/{pl['id']}/videos", params={"cursor": bad})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == cursor.BAD_CURSOR


async def test_my_playlists(client: AsyncClient, db: AsyncSession) -> None:
    owner, other = await api_user(client, db), await api_user(client, db)
    created = {v: await _create(client, owner, v, title=v) for v in PL_VISIBILITIES}
    await _create(client, other)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    await _add(client, owner, created[PL_UNLISTED]["id"], video.id)

    r = await client.get("/api/me/playlists", headers=owner["auth"])
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["visibility"] for i in items] == ["private", "unlisted", "public"]
    assert all(i["contains_video"] is None for i in items)
    unlisted = next(i for i in items if i["visibility"] == "unlisted")
    assert unlisted["video_count"] == 1  # своё приватное видео владельцу видно

    r = await client.get(
        "/api/me/playlists", params={"video_id": str(video.id)}, headers=owner["auth"]
    )
    flags = {i["visibility"]: i["contains_video"] for i in r.json()["items"]}
    assert flags == {"private": False, "unlisted": True, "public": False}

    assert (await client.get("/api/me/playlists")).status_code == 401


# --- Гонки (порядок блокировок, ADR 0006 доп. 7) ---------------------------------------------


async def test_video_delete_waits_for_add(client: AsyncClient, db: AsyncSession) -> None:
    """Добавление держит плейлист и видео (FOR KEY SHARE): удаление видео ждёт его и
    каскадом убирает новый элемент."""
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def add(session: AsyncSession) -> object:
        me = await _me(session, owner)
        return await playlists_service.add_video(session, me, uuid.UUID(pl["id"]), video.id)

    [r] = await waits_for(
        add, [lambda: client.delete(f"/api/videos/{video.id}", headers=owner["auth"])]
    )
    assert r.status_code == 204, r.text
    assert await _items(db, pl["id"]) == []


async def test_add_waits_for_video_delete(client: AsyncClient, db: AsyncSession) -> None:
    """Удаление держит строку видео: добавление ждёт и получает 404, а не нарушение
    внешнего ключа (500)."""
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def delete_video(session: AsyncSession) -> None:
        await manage_service.delete_video(session, await _me(session, owner), video.id)

    [r] = await waits_for(delete_video, [lambda: _add(client, owner, pl["id"], video.id)])
    assert r.status_code == 404, r.text
    assert await _items(db, pl["id"]) == []


async def test_concurrent_adds_get_distinct_positions(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner)
    v1, v2 = await seed_video(db, channel, PUB), await seed_video(db, channel, PUB)

    async def add(session: AsyncSession) -> object:
        me = await _me(session, owner)
        return await playlists_service.add_video(session, me, uuid.UUID(pl["id"]), v1.id)

    [r] = await waits_for(add, [lambda: _add(client, owner, pl["id"], v2.id)])
    assert r.status_code == 201, r.text
    assert r.json()["position"] == 2
    assert [(i.video_id, i.position) for i in await _items(db, pl["id"])] == [
        (v1.id, 1),
        (v2.id, 2),
    ]


async def test_concurrent_adds_respect_limit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_service, "ITEMS_LIMIT", 1)
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner)
    v1, v2 = await seed_video(db, channel, PUB), await seed_video(db, channel, PUB)

    async def add(session: AsyncSession) -> object:
        me = await _me(session, owner)
        return await playlists_service.add_video(session, me, uuid.UUID(pl["id"]), v1.id)

    [r] = await waits_for(add, [lambda: _add(client, owner, pl["id"], v2.id)])
    assert r.status_code == 409, r.text
    assert [i.video_id for i in await _items(db, pl["id"])] == [v1.id]


async def test_concurrent_creates_respect_limit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(playlists_service, "PLAYLISTS_LIMIT", 1)
    owner = await api_user(client, db)

    async def create(session: AsyncSession) -> object:
        data = PlaylistCreateIn(title="первый", visibility=PlaylistVisibility.public)
        return await playlists_service.create(session, await _me(session, owner), data)

    [r] = await waits_for(
        create,
        [
            lambda: client.post(
                "/api/playlists",
                json={"title": "второй", "visibility": "public"},
                headers=owner["auth"],
            )
        ],
    )
    assert r.status_code == 409, r.text
    channel = await channel_of(db, owner)
    count = await db.scalar(
        select(func.count()).select_from(Playlist).where(Playlist.channel_id == channel.id)
    )
    assert count == 1


async def test_add_waits_for_playlist_delete(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def delete(session: AsyncSession) -> None:
        await playlists_service.delete(session, await _me(session, owner), uuid.UUID(pl["id"]))

    [r] = await waits_for(delete, [lambda: _add(client, owner, pl["id"], video.id)])
    assert r.status_code == 404, r.text
    assert await _items(db, pl["id"]) == []


# --- Схема ----------------------------------------------------------------------------------


async def test_schema_constraints(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    pl = await _create(client, owner)
    pid = uuid.UUID(pl["id"])
    channel_id = channel.id
    v1 = (await seed_video(db, channel, PUB)).id
    v2 = (await seed_video(db, channel, PUB)).id
    db.add(PlaylistItem(playlist_id=pid, video_id=v1, position=1))
    await db.commit()
    for video_id, position in (
        (v2, 1),  # позиция занята
        (v1, 2),  # видео уже в плейлисте
        (v2, 0),  # позиция ≥ 1
    ):
        db.add(PlaylistItem(playlist_id=pid, video_id=video_id, position=position))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()  # rollback помечает объекты сессии устаревшими — выше только id
    db.add(Playlist(channel_id=channel_id, title="   ", visibility=PlaylistVisibility.public))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_deleting_user_cascades_playlists(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _add(client, owner, pl["id"], video.id)
    await db.delete(await db.get(User, uuid.UUID(owner["id"])))
    await db.commit()
    for model in (Playlist, PlaylistItem):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_uploading_video_can_be_added(client: AsyncClient, db: AsyncSession) -> None:
    """Своё видео в загрузке тоже добавляется; другим не видно, пока не готово."""
    owner = await api_user(client, db)
    pl = await _create(client, owner)
    video = await seed_video(db, await channel_of(db, owner), PUB, VideoStatus.uploading)
    assert (await _add(client, owner, pl["id"], video.id)).status_code == 201
    assert await _ids(client, None, pl["id"]) == []
    assert await _ids(client, owner, pl["id"]) == [str(video.id)]


# --- Стоимость списков (шаг 2 дня 6) ---------------------------------------------------------


async def test_playlist_lists_page_limit_is_20(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    for url, headers in (
        (f"/api/channels/{owner['username']}/playlists", {}),
        ("/api/me/playlists", owner["auth"]),
    ):
        assert (await client.get(url, params={"limit": 20}, headers=headers)).status_code == 200
        assert (await client.get(url, params={"limit": 21}, headers=headers)).status_code == 422


async def test_first_thumbnail_reads_position_index(db: AsyncSession) -> None:
    """Превью — LATERAL … LIMIT 1 по uq_playlist_items_playlist_position: читается начало
    плейлиста, а не весь состав (generic plan, как у ленты)."""
    from app.repositories import playlists as playlists_repo
    from app.services import policies

    stmt = playlists_repo.first_thumbnails_stmt([uuid.uuid4()], policies.visible_filter(None))
    plan = await generic_plan(db, stmt)
    assert "uq_playlist_items_playlist_position" in plan, plan
    assert "Limit" in plan, plan
