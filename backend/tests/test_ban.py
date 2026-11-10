"""Бан и разбан (ADR 0006 доп. 8).

- кто кого: модератор — только user; админ — user и moderator; админа и себя — никто;
  бан, наложенный админом, модератор не снимает. Все запреты — 403;
- контент забаненного (видео, канал, плейлисты, комментарии) видит только staff;
- его реакции, комментарии и подписки не учитываются в счётчиках (пересчёт при бане
  и разбане); инвариант «счётчик = COUNT по строкам незабаненных» проверяется по всей БД;
- гонки: бан и смена ролей сериализуются; комментарий, реакция, подписка забаненного,
  пришедшие во время бана, отклоняются, а бан ждёт идущие.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.core.errors import Forbidden
from app.models import (
    ModerationAction,
    ModerationLogEntry,
    User,
    UserRole,
    UserSession,
    Video,
    VideoAccess,
)
from app.schemas.playlists import PlaylistCreateIn
from app.services import admin as admin_service
from app.services import bans as bans_service
from app.services import comments as comments_service
from app.services import playlists as playlists_service
from tests.helpers import (
    HIDDEN,
    PASSWORD,
    PRIV,
    PUB,
    READY,
    api_user,
    auth_headers,
    backend_pid,
    channel_of,
    seed_video,
    wait_for_lock_waiters,
    waits_for,
)

ROLES = {"user": UserRole.user, "moderator": UserRole.moderator, "admin": UserRole.admin}


def _ban_url(user_id: str) -> str:
    return f"/api/admin/users/{user_id}/ban"


async def _ban(
    client: AsyncClient, actor: dict | None, target_id: str, reason: str = "спам"
) -> Response:
    return await client.put(
        _ban_url(target_id), json={"reason": reason}, headers=auth_headers(actor)
    )


async def _unban(client: AsyncClient, actor: dict | None, target_id: str) -> Response:
    return await client.delete(_ban_url(target_id), headers=auth_headers(actor))


async def _state(db: AsyncSession, user_id: str) -> tuple[bool, str | None, UserRole | None]:
    await db.rollback()
    row = (
        await db.execute(
            select(User.is_banned, User.banned_reason, User.banned_by_role).where(
                User.id == uuid.UUID(user_id)
            )
        )
    ).one()
    return row[0], row[1], row[2]


async def _log(db: AsyncSession, action: ModerationAction) -> list[ModerationLogEntry]:
    await db.rollback()
    stmt = select(ModerationLogEntry).where(ModerationLogEntry.action == action)
    return list(await db.scalars(stmt.execution_options(populate_existing=True)))


async def _set_banned(db: AsyncSession, user_id: str, by: UserRole) -> None:
    """Бан напрямую в БД (подготовка для тестов разбана)."""
    await db.execute(
        update(User)
        .where(User.id == uuid.UUID(user_id))
        .values(is_banned=True, banned_reason="спам", banned_by_role=by)
    )
    await db.commit()


INCONSISTENT_COUNTERS = text(
    """
    SELECT 'video', v.id FROM videos v
    WHERE v.likes_count <> (SELECT count(*) FROM reactions r JOIN users u ON u.id = r.user_id
                            WHERE r.video_id = v.id AND r.kind = 'like' AND NOT u.is_banned)
       OR v.dislikes_count <> (SELECT count(*) FROM reactions r JOIN users u ON u.id = r.user_id
                               WHERE r.video_id = v.id AND r.kind = 'dislike'
                                 AND NOT u.is_banned)
       OR v.comments_count <> (SELECT count(*) FROM comments c JOIN users u ON u.id = c.author_id
                               WHERE c.video_id = v.id AND NOT u.is_banned)
    UNION ALL
    SELECT 'channel', ch.id FROM channels ch
    WHERE ch.subscribers_count <> (SELECT count(*) FROM subscriptions s
                                   JOIN users u ON u.id = s.user_id
                                   WHERE s.channel_id = ch.id AND NOT u.is_banned)
    """
)


async def assert_counters_consistent(db: AsyncSession) -> None:
    """Инвариант по всей БД: каждый счётчик равен COUNT по строкам незабаненных."""
    await db.rollback()
    bad = (await db.execute(INCONSISTENT_COUNTERS)).all()
    assert bad == [], f"счётчики расходятся со строками: {bad}"


# --- Кто кого банит ---------------------------------------------------------------------------

BAN_EXPECTED = {
    ("moderator", "user"): 200,
    ("moderator", "moderator"): 403,
    ("moderator", "admin"): 403,
    ("moderator", "self"): 403,
    ("admin", "user"): 200,
    ("admin", "moderator"): 200,
    ("admin", "admin"): 403,
    ("admin", "self"): 403,
}


@pytest.mark.parametrize(
    ("actor_kind", "target_kind"),
    [
        (a, t)
        for a in ("anon", "user", "moderator", "admin")
        for t in ("user", "moderator", "admin", "self")
        if (a, t) != ("anon", "self")  # у анонима нет «себя»
    ],
)
async def test_ban_matrix(
    client: AsyncClient, db: AsyncSession, actor_kind: str, target_kind: str
) -> None:
    """Все запреты по ролям — 403 (и «себя», и «админа»); при отказе ничего не меняется."""
    actor = None if actor_kind == "anon" else await api_user(client, db, ROLES[actor_kind])
    if target_kind == "self":
        target = actor
    else:
        target = await api_user(client, db, ROLES[target_kind])

    r = await _ban(client, actor, target["id"])
    expected = (
        401
        if actor is None
        else 403
        if actor_kind == "user"
        else BAN_EXPECTED[(actor_kind, target_kind)]
    )
    assert r.status_code == expected, r.text
    banned, reason, by = await _state(db, target["id"])
    if expected == 200:
        assert (banned, reason, by) == (True, "спам", ROLES[actor_kind])
        assert r.json()["is_banned"] is True
        assert "email" not in r.json()  # модератору email не отдаётся
        [entry] = await _log(db, ModerationAction.user_ban)
        assert entry.reason == "спам" and str(entry.target_id) == target["id"]
        assert entry.details["username"] == target["username"]
    else:
        assert (banned, reason, by) == (False, None, None)
        assert await _log(db, ModerationAction.user_ban) == []


UNBAN_EXPECTED = {
    # (инициатор, цель, кто банил) → код
    ("moderator", "user", "moderator"): 200,
    ("moderator", "user", "admin"): 403,  # бан админа модератор не снимает
    ("moderator", "moderator", "admin"): 403,
    ("admin", "user", "moderator"): 200,
    ("admin", "user", "admin"): 200,
    ("admin", "moderator", "admin"): 200,
}


@pytest.mark.parametrize(
    ("target_kind", "banned_by"),
    [
        ("user", "moderator"),
        ("user", "admin"),
        ("moderator", "admin"),
    ],
)
@pytest.mark.parametrize("actor_kind", ["anon", "user", "moderator", "admin"])
async def test_unban_matrix(
    client: AsyncClient, db: AsyncSession, actor_kind: str, target_kind: str, banned_by: str
) -> None:
    actor = None if actor_kind == "anon" else await api_user(client, db, ROLES[actor_kind])
    target = await api_user(client, db, ROLES[target_kind])
    await _set_banned(db, target["id"], ROLES[banned_by])

    r = await _unban(client, actor, target["id"])
    expected = (
        401
        if actor is None
        else 403
        if actor_kind == "user"
        else UNBAN_EXPECTED[(actor_kind, target_kind, banned_by)]
    )
    assert r.status_code == expected, r.text
    banned, _, by = await _state(db, target["id"])
    if expected == 200:
        assert (banned, by) == (False, None)
        [entry] = await _log(db, ModerationAction.user_unban)
        assert entry.details["banned_by_role"] == banned_by
    else:
        assert (banned, by) == (True, ROLES[banned_by])
        assert await _log(db, ModerationAction.user_unban) == []


async def test_repeat_and_missing(client: AsyncClient, db: AsyncSession) -> None:
    moderator = await api_user(client, db, UserRole.moderator)
    target = await api_user(client, db)
    assert (await _ban(client, moderator, target["id"])).status_code == 200
    r = await _ban(client, moderator, target["id"], reason="другая причина")
    assert r.status_code == 409
    assert await _state(db, target["id"]) == (True, "спам", UserRole.moderator)  # не изменилось
    assert (await _unban(client, moderator, target["id"])).status_code == 200
    assert (await _unban(client, moderator, target["id"])).status_code == 409
    assert (await _ban(client, moderator, str(uuid.uuid4()))).status_code == 404
    assert (await _unban(client, moderator, str(uuid.uuid4()))).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "a\x00"},
        {"reason": "x" * 501},
        {"reason": "x", "role": "admin"},
    ],
)
async def test_ban_reason_validation(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    moderator = await api_user(client, db, UserRole.moderator)
    target = await api_user(client, db)
    r = await client.put(_ban_url(target["id"]), json=payload, headers=moderator["auth"])
    assert r.status_code == 422, r.text
    assert (await _state(db, target["id"]))[0] is False


# --- Смена роли и журнал ----------------------------------------------------------------------


async def test_role_change_of_banned_is_409(client: AsyncClient, db: AsyncSession) -> None:
    admin = await api_user(client, db, UserRole.admin)
    target = await api_user(client, db)
    await _set_banned(db, target["id"], UserRole.moderator)
    r = await client.patch(
        f"/api/admin/users/{target['id']}", json={"role": "admin"}, headers=admin["auth"]
    )
    assert r.status_code == 409, r.text
    await db.rollback()
    assert (
        await db.scalar(select(User.role).where(User.id == uuid.UUID(target["id"])))
        == UserRole.user
    )
    assert await _log(db, ModerationAction.role_change) == []


async def test_role_change_is_logged(client: AsyncClient, db: AsyncSession) -> None:
    admin = await api_user(client, db, UserRole.admin)
    target = await api_user(client, db)
    url = f"/api/admin/users/{target['id']}"
    assert (
        await client.patch(url, json={"role": "moderator"}, headers=admin["auth"])
    ).status_code == 200
    [entry] = await _log(db, ModerationAction.role_change)
    assert str(entry.actor_id) == admin["id"] and str(entry.target_id) == target["id"]
    assert entry.details == {"username": target["username"], "from": "user", "to": "moderator"}
    # Та же роль — без записи; отказ (понизить себя) — без записи.
    assert (
        await client.patch(url, json={"role": "moderator"}, headers=admin["auth"])
    ).status_code == 200
    me = f"/api/admin/users/{admin['id']}"
    assert (await client.patch(me, json={"role": "user"}, headers=admin["auth"])).status_code == 409
    assert len(await _log(db, ModerationAction.role_change)) == 1


# --- Что происходит с контентом -------------------------------------------------------------


async def _world(client: AsyncClient, db: AsyncSession) -> dict:
    """B — будущий забаненный: своё публичное и приватное видео, публичный плейлист;
    лайк, дизлайк, комментарий под видео X и подписка на X. W — подписан на B, лайкнул
    видео B и комментирует под видео X. G — в списке допуска к приватному видео B."""
    b, x, w, g = [await api_user(client, db) for _ in range(4)]
    moderator = await api_user(client, db, UserRole.moderator)
    admin = await api_user(client, db, UserRole.admin)
    b_channel, x_channel = await channel_of(db, b), await channel_of(db, x)
    vb = await seed_video(db, b_channel, PUB)
    vb_private = await seed_video(db, b_channel, PRIV)
    vx, vx2 = await seed_video(db, x_channel, PUB), await seed_video(db, x_channel, PUB)
    vb_id, vb_private_id, vx_id, vx2_id = vb.id, vb_private.id, vx.id, vx2.id
    vb_title = vb.title
    db.add(VideoAccess(video_id=vb_private_id, user_id=uuid.UUID(g["id"])))
    await db.commit()

    async def ok(r: Response, code: int = 200) -> Response:
        assert r.status_code == code, r.text
        return r

    await ok(
        await client.put(f"/api/videos/{vx_id}/reaction", json={"kind": "like"}, headers=b["auth"])
    )
    await ok(
        await client.put(
            f"/api/videos/{vx2_id}/reaction", json={"kind": "dislike"}, headers=b["auth"]
        )
    )
    comment = (
        await ok(
            await client.post(
                f"/api/videos/{vx_id}/comments", json={"body": "от B"}, headers=b["auth"]
            ),
            201,
        )
    ).json()
    await ok(
        await client.post(
            f"/api/videos/{vx_id}/comments", json={"body": "от W"}, headers=w["auth"]
        ),
        201,
    )
    await ok(await client.put(f"/api/channels/{x['username']}/subscription", headers=b["auth"]))
    await ok(await client.put(f"/api/channels/{b['username']}/subscription", headers=w["auth"]))
    await ok(
        await client.put(f"/api/channels/{b['username']}/subscription", headers=moderator["auth"])
    )
    await ok(
        await client.put(f"/api/videos/{vb_id}/reaction", json={"kind": "like"}, headers=w["auth"])
    )
    playlist = (
        await ok(
            await client.post(
                "/api/playlists", json={"title": "B", "visibility": "public"}, headers=b["auth"]
            ),
            201,
        )
    ).json()
    await ok(
        await client.put(f"/api/playlists/{playlist['id']}/videos/{vb_id}", headers=b["auth"]), 201
    )
    await assert_counters_consistent(db)
    return {
        "b": b,
        "x": x,
        "w": w,
        "g": g,
        "moderator": moderator,
        "admin": admin,
        "vb": vb_id,
        "vb_private": vb_private_id,
        "vx": vx_id,
        "vx2": vx2_id,
        "vb_title": vb_title,
        "comment": comment["id"],
        "playlist": playlist["id"],
    }


async def _card(client: AsyncClient, video_id: uuid.UUID, viewer: dict | None) -> Response:
    return await client.get(f"/api/videos/{video_id}", headers=auth_headers(viewer))


async def test_banned_content_hidden_and_counters_recounted(
    client: AsyncClient, db: AsyncSession
) -> None:
    w_ = await _world(client, db)
    b, x, w, g, mod, adm = w_["b"], w_["x"], w_["w"], w_["g"], w_["moderator"], w_["admin"]
    vb, vb_private, vx, vx2 = w_["vb"], w_["vb_private"], w_["vx"], w_["vx2"]

    r = await _ban(client, mod, b["id"])
    assert r.status_code == 200, r.text
    assert r.json()["recount_videos"] == 2 and r.json()["recount_channels"] == 1

    # Сессии удалены, вход — 403 после верного пароля.
    await db.rollback()
    assert (
        await db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.user_id == uuid.UUID(b["id"]))
        )
        == 0
    )
    assert (await client.get("/api/auth/me", headers=b["auth"])).status_code == 401
    r = await client.post("/api/auth/token", data={"username": b["username"], "password": PASSWORD})
    assert r.status_code == 403

    # Видео B: 404 всем, кроме staff (и по допуску тоже).
    for viewer in (None, w, g, x):
        for vid in (vb, vb_private):
            assert (await _card(client, vid, viewer)).status_code == 404
        for suffix in ("/stream", "/comments"):
            r = await client.get(f"/api/videos/{vb}{suffix}", headers=auth_headers(viewer))
            assert r.status_code == 404, suffix
    for viewer in (w, g):
        h = viewer["auth"]
        assert (
            await client.put(f"/api/videos/{vb}/reaction", json={"kind": "like"}, headers=h)
        ).status_code == 404
        assert (
            await client.post(f"/api/videos/{vb}/comments", json={"body": "x"}, headers=h)
        ).status_code == 404
        assert (
            await client.post(f"/api/videos/{vb}/reports", json={"category": "spam"}, headers=h)
        ).status_code == 404
    assert (await client.post(f"/api/videos/{vb}/view")).status_code == 404
    for staff in (mod, adm):
        assert (await _card(client, vb, staff)).status_code == 200
        assert (await _card(client, vb_private, staff)).status_code == 200
        # Staff видит, но не взаимодействует: реакция, комментарий, жалоба, подписка — 409.
        h = staff["auth"]
        r = await client.put(f"/api/videos/{vb}/reaction", json={"kind": "like"}, headers=h)
        assert r.status_code == 409
        r = await client.post(f"/api/videos/{vb}/comments", json={"body": "x"}, headers=h)
        assert r.status_code == 409
        r = await client.post(f"/api/videos/{vb}/reports", json={"category": "spam"}, headers=h)
        assert r.status_code == 409
        r = await client.put(f"/api/channels/{b['username']}/subscription", headers=h)
        assert r.status_code == 409
        # Видео канала — каталог, одинаковый для всех: staff открывает видео по id (из
        # очереди), но список канала у забаненного пуст и для него.
        r = await client.get(f"/api/channels/{b['username']}/videos", headers=h)
        assert r.status_code == 200 and r.json()["items"] == []

    # Каталог (одинаков для всех, включая staff): ни лента, ни поиск.
    for viewer in (None, w, mod):
        feed = await client.get("/api/videos", headers=auth_headers(viewer))
        assert str(vb) not in {i["id"] for i in feed.json()["items"]}
        search = await client.get(
            "/api/videos", params={"q": w_["vb_title"]}, headers=auth_headers(viewer)
        )
        assert str(vb) not in {i["id"] for i in search.json()["items"]}
        assert str(vx) in {i["id"] for i in search.json()["items"]}

    # Канал, его видео и плейлисты, подписка: 404 всем, кроме staff.
    ch = f"/api/channels/{b['username']}"
    for viewer in (None, w):
        for suffix in ("", "/videos", "/playlists"):
            assert (await client.get(ch + suffix, headers=auth_headers(viewer))).status_code == 404
        assert (
            await client.get(f"/api/playlists/{w_['playlist']}", headers=auth_headers(viewer))
        ).status_code == 404
    for verb in ("put", "delete"):
        r = await getattr(client, verb)(f"{ch}/subscription", headers=w["auth"])
        assert r.status_code == 404
    assert (await client.get(ch, headers=mod["auth"])).status_code == 200
    assert (
        await client.get(f"/api/playlists/{w_['playlist']}", headers=mod["auth"])
    ).status_code == 200
    # «Мои подписки»: W не видит канал B, модератор видит.
    subs = await client.get("/api/me/subscriptions", headers=w["auth"])
    assert b["username"] not in {i["channel"]["handle"] for i in subs.json()["items"]}
    subs = await client.get("/api/me/subscriptions", headers=mod["auth"])
    assert b["username"] in {i["channel"]["handle"] for i in subs.json()["items"]}

    # Комментарий B под видео X: скрыт, staff видит с пометкой.
    page = (await client.get(f"/api/videos/{vx}/comments", headers=w["auth"])).json()["items"]
    assert [c["body"] for c in page] == ["от W"]
    page = (await client.get(f"/api/videos/{vx}/comments", headers=mod["auth"])).json()["items"]
    by_body = {c["body"]: c for c in page}
    assert by_body["от B"]["author_banned"] is True
    assert by_body["от B"]["author_user_id"] == b["id"]
    assert by_body["от W"]["author_banned"] is False

    # Счётчики: лайк, дизлайк, комментарий и подписка B не учитываются.
    card = (await _card(client, vx, w)).json()
    assert (card["likes_count"], card["comments_count"]) == (0, 1)
    assert (await _card(client, vx2, w)).json()["dislikes_count"] == 0
    assert (await client.get(f"/api/channels/{x['username']}")).json()["subscribers_count"] == 0
    await assert_counters_consistent(db)

    # Разбан — всё возвращается.
    assert (await _unban(client, mod, b["id"])).status_code == 200
    await assert_counters_consistent(db)
    card = (await _card(client, vx, w)).json()
    assert (card["likes_count"], card["comments_count"]) == (1, 2)
    assert (await _card(client, vx2, w)).json()["dislikes_count"] == 1
    assert (await client.get(f"/api/channels/{x['username']}")).json()["subscribers_count"] == 1
    assert (await _card(client, vb, None)).status_code == 200
    assert (await _card(client, vb_private, g)).status_code == 200
    assert (await client.get(ch)).status_code == 200
    assert (await client.get(f"/api/playlists/{w_['playlist']}")).status_code == 200
    page = (await client.get(f"/api/videos/{vx}/comments", headers=w["auth"])).json()["items"]
    assert sorted(c["body"] for c in page) == ["от B", "от W"]
    # Сессии не восстановлены: старый токен не действует, вход — заново.
    assert (await client.get("/api/auth/me", headers=b["auth"])).status_code == 401
    r = await client.post("/api/auth/token", data={"username": b["username"], "password": PASSWORD})
    assert r.status_code == 200


async def test_author_user_id_only_for_staff(client: AsyncClient, db: AsyncSession) -> None:
    owner, author = await api_user(client, db), await api_user(client, db)
    mod = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(
        f"/api/videos/{video.id}/comments", json={"body": "привет"}, headers=author["auth"]
    )
    assert r.status_code == 201
    assert r.json()["author_user_id"] is None and r.json()["author_banned"] is None
    for viewer in (None, owner, author):
        [c] = (
            await client.get(f"/api/videos/{video.id}/comments", headers=auth_headers(viewer))
        ).json()["items"]
        assert c["author_user_id"] is None and c["author_banned"] is None
    [c] = (await client.get(f"/api/videos/{video.id}/comments", headers=mod["auth"])).json()[
        "items"
    ]
    assert c["author_user_id"] == author["id"] and c["author_banned"] is False


async def test_deleting_banned_authors_comment_keeps_counter(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Комментарий забаненного не-staff не видит: удаление владельцем видео и посторонним —
    404, как у несуществующего. Staff удаляет — счётчик не уменьшается (он не учтён)."""
    w_ = await _world(client, db)
    assert (await _ban(client, w_["moderator"], w_["b"]["id"])).status_code == 200
    missing = await client.delete(f"/api/comments/{uuid.uuid4()}", headers=w_["x"]["auth"])
    for viewer in (w_["x"], w_["w"]):
        r = await client.delete(f"/api/comments/{w_['comment']}", headers=viewer["auth"])
        assert r.status_code == 404
        assert r.json() == missing.json()
    r = await client.delete(f"/api/comments/{w_['comment']}", headers=w_["admin"]["auth"])
    assert r.status_code == 204
    assert (await _card(client, w_["vx"], None)).json()["comments_count"] == 1
    await assert_counters_consistent(db)


async def test_queue_shows_owner_for_ban(client: AsyncClient, db: AsyncSession) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    mod = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(
        f"/api/videos/{video.id}/reports", json={"category": "spam"}, headers=reporter["auth"]
    )
    assert r.status_code == 201
    [item] = (await client.get("/api/admin/reports", headers=mod["auth"])).json()["items"]
    assert item["video"]["owner_id"] == owner["id"] and item["video"]["owner_banned"] is False
    assert (await _ban(client, mod, item["video"]["owner_id"])).status_code == 200
    [item] = (await client.get("/api/admin/reports", headers=mod["auth"])).json()["items"]
    assert item["video"]["owner_banned"] is True


# --- Гонки ---------------------------------------------------------------------------------


async def _me(session: AsyncSession, user: dict) -> User:
    return await session.get(User, uuid.UUID(user["id"]))


async def test_ban_waits_for_promotion(client: AsyncClient, db: AsyncSession) -> None:
    """Админ повышает цель до модератора, модератор её банит: бан ждёт смену роли и видит
    уже модератора — 403."""
    admin, mod = (
        await api_user(client, db, UserRole.admin),
        await api_user(client, db, UserRole.moderator),
    )
    target = await api_user(client, db)

    async def promote(session: AsyncSession) -> object:
        return await admin_service.set_role(
            session, await _me(session, admin), uuid.UUID(target["id"]), UserRole.moderator
        )

    [r] = await waits_for(promote, [lambda: _ban(client, mod, target["id"])])
    assert r.status_code == 403, r.text
    assert (await _state(db, target["id"]))[0] is False


async def test_ban_waits_for_actor_demotion(client: AsyncClient, db: AsyncSession) -> None:
    """Инициатора понижают до user, пока его бан ждёт: роль перечитывается под блокировкой — 403."""
    admin, mod = (
        await api_user(client, db, UserRole.admin),
        await api_user(client, db, UserRole.moderator),
    )
    target = await api_user(client, db)

    async def demote(session: AsyncSession) -> object:
        return await admin_service.set_role(
            session, await _me(session, admin), uuid.UUID(mod["id"]), UserRole.user
        )

    [r] = await waits_for(demote, [lambda: _ban(client, mod, target["id"])])
    assert r.status_code == 403, r.text
    assert (await _state(db, target["id"]))[0] is False


async def test_ban_by_initiator_banned_while_waiting(client: AsyncClient, db: AsyncSession) -> None:
    """Админ банит модератора M, пока запрос M «забанить U» ждёт: роль M перечитывается
    только у незабаненного — 403, U не забанен (ревью и аудит шага 3)."""
    admin, mod = (
        await api_user(client, db, UserRole.admin),
        await api_user(client, db, UserRole.moderator),
    )
    target = await api_user(client, db)

    async def ban_mod(session: AsyncSession) -> object:
        return await bans_service.ban(
            session, await _me(session, admin), uuid.UUID(mod["id"]), "злоупотребление"
        )

    [r] = await waits_for(ban_mod, [lambda: _ban(client, mod, target["id"])])
    assert r.status_code in (401, 403), r.text  # сессия M удалена или роль не прочитана
    assert (await _state(db, target["id"]))[0] is False


async def test_ban_request_with_preloaded_actor_rejected_after_actor_banned(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Тот же случай на уровне сервиса (без проверки сессии в authenticate): инициатор
    забанен до того, как его бан получил блокировку, — 403."""
    admin, mod = (
        await api_user(client, db, UserRole.admin),
        await api_user(client, db, UserRole.moderator),
    )
    target = await api_user(client, db)
    assert (await _ban(client, admin, mod["id"])).status_code == 200
    me = await db.get(User, uuid.UUID(mod["id"]))  # объект «до бана» — роль moderator
    with pytest.raises(Forbidden):
        await bans_service.ban(db, me, uuid.UUID(target["id"]), "спам")
    assert (await _state(db, target["id"]))[0] is False


async def test_fk_insert_during_ban_does_not_deadlock(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Путь «видео → вставка со ссылкой на пользователя» (жалоба, допуск, журнал) против
    бана «пользователь → видео». Цель бана — FOR NO KEY UPDATE: FOR KEY SHARE проверки
    внешнего ключа с ним совместим, взаимоблокировки нет (с FOR UPDATE была бы 40P01)."""
    w_ = await _world(client, db)
    b_id, vx = uuid.UUID(w_["b"]["id"]), w_["vx"]  # у B есть комментарий и лайк под vx
    async with get_sessionmaker()() as s1:
        # Как жалоба: строка видео FOR SHARE, затем INSERT со ссылкой на пользователя.
        await s1.execute(select(Video.id).where(Video.id == vx).with_for_update(read=True))
        pid = await backend_pid(s1)
        ban_task = asyncio.create_task(_ban(client, w_["moderator"], w_["b"]["id"]))
        await wait_for_lock_waiters(pid)  # бан взял строку B и ждёт vx
        await s1.execute(insert(VideoAccess).values(video_id=vx, user_id=b_id))
        await s1.commit()
    r = await ban_task
    assert r.status_code == 200, r.text
    await assert_counters_consistent(db)


async def test_login_during_ban_is_rejected(client: AsyncClient, db: AsyncSession) -> None:
    """Вход, пришедший во время бана, ждёт его и получает 403; сессий у забаненного нет."""
    mod, b = await api_user(client, db, UserRole.moderator), await api_user(client, db)

    async def ban(session: AsyncSession) -> object:
        return await bans_service.ban(session, await _me(session, mod), uuid.UUID(b["id"]), "спам")

    login = lambda: client.post(  # noqa: E731
        "/api/auth/token", data={"username": b["username"], "password": PASSWORD}
    )
    [r] = await waits_for(ban, [login])
    assert r.status_code == 403, r.text
    await db.rollback()
    count = await db.scalar(
        select(func.count())
        .select_from(UserSession)
        .where(UserSession.user_id == uuid.UUID(b["id"]))
    )
    assert count == 0


async def test_role_change_waits_for_ban(client: AsyncClient, db: AsyncSession) -> None:
    """Бан идёт — повышение ждёт его и получает 409 (забаненному роль не меняется)."""
    admin, mod = (
        await api_user(client, db, UserRole.admin),
        await api_user(client, db, UserRole.moderator),
    )
    target = await api_user(client, db)

    async def ban(session: AsyncSession) -> object:
        return await bans_service.ban(
            session, await _me(session, mod), uuid.UUID(target["id"]), "спам"
        )

    [r] = await waits_for(
        ban,
        [
            lambda: client.patch(
                f"/api/admin/users/{target['id']}", json={"role": "admin"}, headers=admin["auth"]
            )
        ],
    )
    assert r.status_code == 409, r.text


async def _hold_ban(world: dict):
    async def ban(session: AsyncSession) -> object:
        return await bans_service.ban(
            session, await _me(session, world["moderator"]), uuid.UUID(world["b"]["id"]), "спам"
        )

    return ban


@pytest.mark.parametrize("action", ["comment", "reaction", "subscribe", "unsubscribe"])
async def test_banned_users_request_waits_for_ban(
    client: AsyncClient, db: AsyncSession, action: str
) -> None:
    """Запрос B, начатый во время бана, ждёт его и получает 401; счётчики сходятся.
    Видео и канал — новые для B: бан их строки не блокирует, защищает только блокировка
    строки пользователя (lock_self_active)."""
    w_ = await _world(client, db)
    b, y = w_["b"], await api_user(client, db)
    vy = (await seed_video(db, await channel_of(db, y), PUB)).id
    if action == "unsubscribe":
        r = await client.put(f"/api/channels/{y['username']}/subscription", headers=b["auth"])
        assert r.status_code == 200
    requests = {
        "comment": lambda: client.post(
            f"/api/videos/{vy}/comments", json={"body": "в полёте"}, headers=b["auth"]
        ),
        "reaction": lambda: client.put(
            f"/api/videos/{vy}/reaction", json={"kind": "like"}, headers=b["auth"]
        ),
        "subscribe": lambda: client.put(
            f"/api/channels/{y['username']}/subscription", headers=b["auth"]
        ),
        "unsubscribe": lambda: client.delete(
            f"/api/channels/{y['username']}/subscription", headers=b["auth"]
        ),
    }
    [r] = await waits_for(await _hold_ban(w_), [requests[action]])
    assert r.status_code == 401, r.text
    await assert_counters_consistent(db)


async def test_ban_waits_for_comment_in_flight(client: AsyncClient, db: AsyncSession) -> None:
    """Комментарий B под новым для него видео идёт — бан ждёт его (строка пользователя FOR
    SHARE) и пересчитывает с ним: счётчики сходятся."""
    w_ = await _world(client, db)
    y = await api_user(client, db)
    vy = (await seed_video(db, await channel_of(db, y), PUB)).id

    async def comment(session: AsyncSession) -> object:
        return await comments_service.add_comment(
            session, await _me(session, w_["b"]), vy, "в полёте"
        )

    [r] = await waits_for(comment, [lambda: _ban(client, w_["moderator"], w_["b"]["id"])])
    assert r.status_code == 200, r.text
    assert r.json()["recount_videos"] == 3  # vx, vx2 и новое vy
    assert (await _card(client, vy, None)).json()["comments_count"] == 0
    await assert_counters_consistent(db)


# --- Схема ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {"is_banned": True},  # без причины и роли
        {"is_banned": True, "banned_reason": "спам"},  # без роли
        {"banned_reason": "спам"},  # причина у незабаненного
        {"is_banned": True, "banned_reason": "   ", "banned_by_role": UserRole.admin},
        {"is_banned": True, "banned_reason": "x" * 501, "banned_by_role": UserRole.admin},
    ],
)
async def test_user_ban_check(client: AsyncClient, db: AsyncSession, values: dict) -> None:
    user = await api_user(client, db)
    with pytest.raises(IntegrityError):
        await db.execute(update(User).where(User.id == uuid.UUID(user["id"])).values(**values))
    await db.rollback()


@pytest.mark.parametrize(
    "values",
    [
        {"moderation": HIDDEN, "moderation_reason": None},
        {"moderation_reason": "причина у не скрытого"},
        {"moderation": HIDDEN, "moderation_reason": "  "},
        {"moderation": HIDDEN, "moderation_reason": "x" * 501},
    ],
)
async def test_moderation_reason_check(client: AsyncClient, db: AsyncSession, values: dict) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB, READY)
    vid = video.id
    with pytest.raises(IntegrityError):
        await db.execute(update(Video).where(Video.id == vid).values(**values))
    await db.rollback()


async def test_banned_owner_playlist_hidden_not_deleted(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Плейлисты забаненного остаются в БД и возвращаются при разбане (скрытие, не удаление)."""
    owner = await api_user(client, db)
    mod = await api_user(client, db, UserRole.moderator)
    me = await db.get(User, uuid.UUID(owner["id"]))
    out = await playlists_service.create(db, me, PlaylistCreateIn(title="p", visibility="public"))
    pid = out.id
    assert (await _ban(client, mod, owner["id"])).status_code == 200
    assert (await client.get(f"/api/playlists/{pid}")).status_code == 404
    assert (await _unban(client, mod, owner["id"])).status_code == 200
    assert (await client.get(f"/api/playlists/{pid}")).status_code == 200
