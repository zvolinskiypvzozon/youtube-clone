"""Подписки: PUT/DELETE /api/channels/{handle}/subscription, флаг `subscribed` в канале,
GET /api/me/subscriptions.

Подписка на свой канал — 400; повторы идемпотентны; subscribers_count сходится при
параллельных подписках и отписках. Подписка не открывает приватные видео канала. Кто
подписан на канал, наружу не отдаётся — только счётчик; свои подписки видит только сам.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models import Channel, Subscription, User, VideoAccess
from app.services import subscriptions as subscriptions_service
from tests.helpers import PRIV, PUB, UNL, api_user, channel_of, seed_video


def _url(handle: str) -> str:
    return f"/api/channels/{handle}/subscription"


async def _state(db: AsyncSession, channel_id: uuid.UUID) -> tuple[int, int]:
    """(channels.subscribers_count, фактическое число строк subscriptions)."""
    counter = await db.scalar(select(Channel.subscribers_count).where(Channel.id == channel_id))
    rows = await db.scalar(select(func.count()).where(Subscription.channel_id == channel_id))
    await db.commit()
    return counter or 0, rows or 0


# --- Подписка и отписка ----------------------------------------------------------------------


async def test_subscribe_and_unsubscribe(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    handle = owner["username"]

    r = await client.put(_url(handle), headers=user["auth"])
    assert r.status_code == 200, r.text
    assert r.json() == {"subscribed": True, "subscribers_count": 1}
    # Повтор ничего не меняет.
    r = await client.put(_url(handle), headers=user["auth"])
    assert r.json() == {"subscribed": True, "subscribers_count": 1}
    assert await _state(db, channel.id) == (1, 1)

    r = await client.delete(_url(handle), headers=user["auth"])
    assert r.status_code == 200, r.text
    assert r.json() == {"subscribed": False, "subscribers_count": 0}
    r = await client.delete(_url(handle), headers=user["auth"])  # идемпотентно
    assert r.json() == {"subscribed": False, "subscribers_count": 0}
    assert await _state(db, channel.id) == (0, 0)


async def test_subscribe_to_own_channel_is_400(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    r = await client.put(_url(owner["username"]), headers=owner["auth"])
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == subscriptions_service.SELF_SUBSCRIPTION
    assert await _state(db, channel.id) == (0, 0)
    # Отписка от своего канала — идемпотентный 200 без изменений.
    r = await client.delete(_url(owner["username"]), headers=owner["auth"])
    assert r.json() == {"subscribed": False, "subscribers_count": 0}


async def test_channel_flag_subscribed(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    user, other = await api_user(client, db), await api_user(client, db)
    handle = owner["username"]
    await client.put(_url(handle), headers=user["auth"])

    def flag(r) -> object:
        assert r.status_code == 200, r.text
        return r.json()["subscribed"]

    assert flag(await client.get(f"/api/channels/{handle}")) is None  # аноним
    assert flag(await client.get(f"/api/channels/{handle}", headers=user["auth"])) is True
    assert flag(await client.get(f"/api/channels/{handle}", headers=other["auth"])) is False
    assert flag(await client.get(f"/api/channels/{handle}", headers=owner["auth"])) is False
    assert (await client.get(f"/api/channels/{handle}")).json()["subscribers_count"] == 1


async def test_unknown_channel_and_case_insensitive_handle(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    assert (await client.put(_url("no_such_channel"), headers=user["auth"])).status_code == 404
    assert (await client.delete(_url("no_such_channel"), headers=user["auth"])).status_code == 404
    r = await client.put(_url(owner["username"].upper()), headers=user["auth"])
    assert r.status_code == 200, r.text
    assert await _state(db, channel.id) == (1, 1)


async def test_requires_login(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    assert (await client.put(_url(owner["username"]))).status_code == 401
    assert (await client.delete(_url(owner["username"]))).status_code == 401
    assert (await client.get("/api/me/subscriptions")).status_code == 401


async def test_subscription_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    s = uuid.uuid4().hex[:8]
    await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    assert (await client.put(_url(owner["username"]))).status_code == 403
    csrf = {"X-CSRF-Token": client.cookies["csrf"]}
    assert (await client.put(_url(owner["username"]), headers=csrf)).status_code == 200
    assert (await client.delete(_url(owner["username"]))).status_code == 403
    assert (await client.delete(_url(owner["username"]), headers=csrf)).status_code == 200


async def test_cascade_on_subscriber_delete(client: AsyncClient, db: AsyncSession) -> None:
    """Удаление подписчика удаляет его подписки (users → subscriptions). Счётчик канала при
    этом не уменьшается — пересчёт на дне 6 (путь удаления пользователя), здесь не проверяется."""
    owner, user = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    await client.put(_url(owner["username"]), headers=user["auth"])
    await db.execute(delete(User).where(User.id == uuid.UUID(user["id"])))
    await db.commit()
    assert (await _state(db, channel.id))[1] == 0


async def test_cascade_on_channel_owner_delete(client: AsyncClient, db: AsyncSession) -> None:
    """Удаление владельца канала: users → channels → subscriptions."""
    owner, user = await api_user(client, db), await api_user(client, db)
    await client.put(_url(owner["username"]), headers=user["auth"])
    await db.execute(delete(User).where(User.id == uuid.UUID(owner["id"])))
    await db.commit()
    n = await db.scalar(select(func.count()).select_from(Subscription))
    await db.commit()
    assert n == 0
    assert (await client.get("/api/me/subscriptions", headers=user["auth"])).json()["items"] == []


# --- Мои подписки ----------------------------------------------------------------------------


async def test_my_subscriptions_pagination(client: AsyncClient, db: AsyncSession) -> None:
    me = await api_user(client, db)
    owners = [await api_user(client, db) for _ in range(5)]
    for o in owners:
        assert (await client.put(_url(o["username"]), headers=me["auth"])).status_code == 200

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        r = await client.get("/api/me/subscriptions", params=params, headers=me["auth"])
        assert r.status_code == 200, r.text
        page = r.json()
        seen += [item["channel"]["handle"] for item in page["items"]]
        assert all(item["subscribers_count"] == 1 for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [o["username"] for o in owners][::-1]  # новые сверху

    r = await client.get("/api/me/subscriptions", params={"cursor": "мусор"}, headers=me["auth"])
    assert r.status_code == 400


async def test_my_subscriptions_keyset_with_equal_timestamps(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Одинаковый created_at: порядок и стык страниц решает channel_id (tie-breaker)."""
    me = await api_user(client, db)
    owners = [await api_user(client, db) for _ in range(5)]
    same = datetime(2026, 1, 1, tzinfo=UTC)
    channel_ids = [(await channel_of(db, o)).id for o in owners]
    db.add_all(
        Subscription(channel_id=cid, user_id=uuid.UUID(me["id"]), created_at=same)
        for cid in channel_ids
    )
    await db.commit()

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/api/me/subscriptions", params=params, headers=me["auth"])).json()
        seen += [item["channel"]["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(c) for c in sorted(channel_ids, reverse=True)]


# --- Приватность списков ---------------------------------------------------------------------


async def test_subscribers_and_others_subscriptions_are_not_exposed(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Кто подписан на канал, наружу не отдаётся — только счётчик; свои подписки видит
    только сам пользователь: у /api/me/subscriptions нет параметра «чей список»."""
    owner = await api_user(client, db)
    alice, bob = await api_user(client, db), await api_user(client, db)
    handle = owner["username"]
    await client.put(_url(handle), headers=alice["auth"])

    # Канал: только счётчик, ни id, ни ников подписчиков — даже владельцу.
    for headers in ({}, owner["auth"], bob["auth"]):
        r = await client.get(f"/api/channels/{handle}", headers=headers)
        body = r.text
        assert r.json()["subscribers_count"] == 1
        assert alice["id"] not in body and alice["username"] not in body

    # Чужие подписки не видны: Боб получает свой (пустой) список, параметры игнорируются.
    for params in ({}, {"user_id": alice["id"]}, {"username": alice["username"]}):
        r = await client.get("/api/me/subscriptions", params=params, headers=bob["auth"])
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []
    mine = await client.get("/api/me/subscriptions", headers=alice["auth"])
    assert [i["channel"]["handle"] for i in mine.json()["items"]] == [handle]

    # Эндпоинта со списком подписчиков нет ни в API, ни в схеме.
    for path in (f"/api/channels/{handle}/subscribers", f"/api/channels/{handle}/subscriptions"):
        assert (await client.get(path, headers=owner["auth"])).status_code in (404, 405)
    paths = app.openapi()["paths"]
    exposed = [p for p in paths if "subscri" in p]
    assert sorted(exposed) == ["/api/channels/{handle}/subscription", "/api/me/subscriptions"]
    assert set(paths["/api/channels/{handle}/subscription"]) == {"put", "delete"}


async def test_subscription_does_not_open_private_videos(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Подписка не участвует в политике доступа: приватное видео канала подписчику не видно
    ни по ссылке, ни в списке канала, ни через комментарии; unlisted — только по ссылке."""
    owner, fan = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    public = await seed_video(db, channel, PUB)
    unlisted = await seed_video(db, channel, UNL)
    private = await seed_video(db, channel, PRIV)
    assert (await client.put(_url(owner["username"]), headers=fan["auth"])).status_code == 200

    for path in ("", "/stream", "/comments"):
        r = await client.get(f"/api/videos/{private.id}{path}", headers=fan["auth"])
        assert r.status_code == 404, (path, r.text)
    listed = await client.get(f"/api/channels/{owner['username']}/videos", headers=fan["auth"])
    assert [v["id"] for v in listed.json()["items"]] == [str(public.id)]
    r = await client.get(f"/api/videos/{unlisted.id}", headers=fan["auth"])
    assert r.status_code == 200  # unlisted — по ссылке всем, подписка тут ни при чём

    # Контроль: тот же подписчик с допуском видит приватное — значит 404 выше от политики.
    db.add(VideoAccess(video_id=private.id, user_id=uuid.UUID(fan["id"])))
    await db.commit()
    assert (await client.get(f"/api/videos/{private.id}", headers=fan["auth"])).status_code == 200


# --- Гонки -----------------------------------------------------------------------------------


async def test_concurrent_subscriptions_of_many_users(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    users = [await api_user(client, db) for _ in range(20)]
    rs = await asyncio.gather(
        *(client.put(_url(owner["username"]), headers=u["auth"]) for u in users)
    )
    assert {r.status_code for r in rs} == {200}
    assert await _state(db, channel.id) == (20, 20)

    rs = await asyncio.gather(
        *(client.delete(_url(owner["username"]), headers=u["auth"]) for u in users[:12])
    )
    assert {r.status_code for r in rs} == {200}
    assert await _state(db, channel.id) == (8, 8)


async def test_concurrent_repeats_of_one_user_count_once(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    rs = await asyncio.gather(
        *(client.put(_url(owner["username"]), headers=user["auth"]) for _ in range(10))
    )
    assert {r.status_code for r in rs} == {200}
    # И в ответах проигравших счётчик свежий: «подписан, подписчиков 0» не бывает.
    assert {r.json()["subscribers_count"] for r in rs} == {1}
    assert await _state(db, channel.id) == (1, 1)

    rs = await asyncio.gather(
        *(client.delete(_url(owner["username"]), headers=user["auth"]) for _ in range(10))
    )
    assert {r.status_code for r in rs} == {200}
    assert {r.json()["subscribers_count"] for r in rs} == {0}
    assert await _state(db, channel.id) == (0, 0)


async def test_concurrent_subscribe_and_unsubscribe_mix(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Подписки и отписки нескольких пользователей вперемешку и параллельно: в конце
    счётчик равен числу строк (какой запрос «победил» у каждого — неважно)."""
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    users = [await api_user(client, db) for _ in range(6)]
    for _ in range(3):
        calls = []
        for u in users:
            calls += [
                client.put(_url(owner["username"]), headers=u["auth"]),
                client.delete(_url(owner["username"]), headers=u["auth"]),
                client.put(_url(owner["username"]), headers=u["auth"]),
            ]
        rs = await asyncio.gather(*calls)
        assert {r.status_code for r in rs} == {200}
        counter, rows = await _state(db, channel.id)
        assert counter == rows
