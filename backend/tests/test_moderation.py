"""Модерация (staff): очередь жалоб, скрытие и возврат видео, отклонение жалобы, удаление
комментария staff (ADR 0006 доп. 5 и 6).

Жалобы видит только staff: владелец видео (и любой user) получает 403 на очередь, а в своих
ответах видит только причину скрытия. Порядок блокировок «видео → жалобы»: скрытие ждёт
жалобу, держащую строку видео, и закрывает её; отклонение ждёт удаление видео без
взаимоблокировки.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import (
    Comment,
    ModerationAction,
    ModerationLogEntry,
    ModerationState,
    Report,
    ReportCategory,
    ReportStatus,
    User,
    UserRole,
    Video,
    VideoAccess,
)
from app.repositories import reports as reports_repo
from app.repositories import videos as videos_repo
from app.services import manage as manage_service
from app.services import moderation as moderation_service
from app.services import policies
from app.services import videos as videos_service
from tests.helpers import (
    HIDDEN,
    PRIV,
    PROC,
    PUB,
    READY,
    api_user,
    channel_of,
    seed_video,
    waits_for,
)

SPAM = {"category": "spam"}
QUEUE = "/api/admin/reports"


def _mod_url(video_id: uuid.UUID) -> str:
    return f"/api/admin/videos/{video_id}/moderation"


def _dismiss_url(report_id: str | uuid.UUID) -> str:
    return f"/api/admin/reports/{report_id}/dismiss"


async def _report(client: AsyncClient, user: dict, video_id: uuid.UUID, **extra: str) -> str:
    r = await client.post(
        f"/api/videos/{video_id}/reports", json={**SPAM, **extra}, headers=user["auth"]
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _hide(client: AsyncClient, staff: dict, video_id: uuid.UUID, reason: str = "спам"):
    return await client.put(
        _mod_url(video_id), json={"state": "hidden", "reason": reason}, headers=staff["auth"]
    )


async def _log(db: AsyncSession, target_id: uuid.UUID | str) -> list[ModerationLogEntry]:
    stmt = (
        select(ModerationLogEntry)
        .where(ModerationLogEntry.target_id == uuid.UUID(str(target_id)))
        .order_by(ModerationLogEntry.created_at)
    )
    rows = list(await db.scalars(stmt))
    await db.commit()
    return rows


async def _get_report(db: AsyncSession, report_id: str | uuid.UUID) -> Report:
    report = await db.scalar(
        select(Report)
        .where(Report.id == uuid.UUID(str(report_id)))
        .execution_options(populate_existing=True)
    )
    await db.commit()
    assert report is not None
    return report


async def _queue_ids(client: AsyncClient, staff: dict, **params: str) -> list[str]:
    r = await client.get(QUEUE, params=params, headers=staff["auth"])
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()["items"]]


# --- Очередь жалоб: только staff ------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "expected"),
    [("anon", 401), ("user", 403), ("video_owner", 403), ("moderator", 200), ("admin", 200)],
)
async def test_queue_is_staff_only(
    client: AsyncClient, db: AsyncSession, who: str, expected: int
) -> None:
    """Жалобы видит только staff: владелец видео, на которое пожаловались, получает 403
    и не узнаёт ни жалоб, ни пожаловавшихся."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _report(client, reporter, video.id, comment="маркер-жалобы")
    viewers = {
        "anon": None,
        "user": await api_user(client, db),
        "video_owner": owner,
        "moderator": await api_user(client, db, UserRole.moderator),
        "admin": await api_user(client, db, UserRole.admin),
    }
    viewer = viewers[who]
    for status in ("open", "resolved", "dismissed"):
        r = await client.get(
            QUEUE, params={"status": status}, headers=viewer["auth"] if viewer else {}
        )
        assert r.status_code == expected, (status, r.text)
        if expected != 200:
            assert reporter["username"] not in r.text and "маркер-жалобы" not in r.text


async def test_queue_contents_and_snapshot(client: AsyncClient, db: AsyncSession) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    report_id = await _report(client, reporter, video.id, comment="реклама казино")

    r = await client.get(QUEUE, headers=moderator["auth"])
    assert r.status_code == 200, r.text
    [item] = [i for i in r.json()["items"] if i["id"] == report_id]
    assert item["category"] == "spam"
    assert item["comment"] == "реклама казино"
    assert item["status"] == "open"
    assert item["reporter"] == {"id": reporter["id"], "username": reporter["username"]}
    assert item["video"] == {
        "id": str(video.id),
        "title": video.title,
        "channel_handle": owner["username"],
        "moderation": "ok",
        "visibility": "public",
        "status": "ready",
        "owner_id": owner["id"],  # для бана из очереди (ADR 0006 доп. 8)
        "owner_banned": False,
    }
    assert item["resolved_by"] is None and item["resolved_at"] is None
    assert r.headers["cache-control"] == "no-store"


async def test_queue_after_video_deleted(client: AsyncClient, db: AsyncSession) -> None:
    """Видео удалено владельцем: жалоба остаётся в очереди со снимком, текущего состояния нет."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    title = video.title
    report_id = await _report(client, reporter, video.id)
    assert (
        await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    ).status_code == 204

    r = await client.get(QUEUE, headers=moderator["auth"])
    [item] = [i for i in r.json()["items"] if i["id"] == report_id]
    assert item["video"] == {
        "id": None,
        "title": title,
        "channel_handle": owner["username"],
        "moderation": None,
        "visibility": None,
        "status": None,
        "owner_id": None,
        "owner_banned": None,
    }


async def test_queue_status_filter_and_pagination(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    # Свои жалобы — на отдельном статусе, чтобы чужие тесты не мешали порядку.
    ids = [await _report(client, await api_user(client, db), video.id) for _ in range(3)]
    await db.execute(
        update(Report)
        .where(Report.id.in_([uuid.UUID(i) for i in ids]))
        .values(status=ReportStatus.dismissed, resolved_at=func.now())
    )
    await db.commit()
    assert not set(ids) & set(await _queue_ids(client, moderator))  # не в открытых

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"status": "dismissed", "limit": "2", **({"cursor": cursor} if cursor else {})}
        page = (await client.get(QUEUE, params=params, headers=moderator["auth"])).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None or set(ids) <= set(seen):
            break
    mine = [i for i in seen if i in ids]
    assert mine == list(reversed(ids))  # новые сверху, без пропусков и повторов


async def test_queue_bad_cursor_is_400(client: AsyncClient, db: AsyncSession) -> None:
    moderator = await api_user(client, db, UserRole.moderator)
    r = await client.get(QUEUE, params={"cursor": "мусор"}, headers=moderator["auth"])
    assert r.status_code == 400, r.text


# --- Скрытие и возврат -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "expected"),
    [("anon", 401), ("user", 403), ("video_owner", 403), ("moderator", 200), ("admin", 200)],
)
async def test_hide_is_staff_only(
    client: AsyncClient, db: AsyncSession, who: str, expected: int
) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    actor = {
        "anon": None,
        "user": await api_user(client, db),
        "video_owner": owner,
        "moderator": await api_user(client, db, UserRole.moderator),
        "admin": await api_user(client, db, UserRole.admin),
    }[who]
    r = await client.put(
        _mod_url(video.id),
        json={"state": "hidden", "reason": "x"},
        headers=actor["auth"] if actor else {},
    )
    assert r.status_code == expected, r.text
    moderation = await db.scalar(
        select(Video.moderation)
        .where(Video.id == video.id)
        .execution_options(populate_existing=True)
    )
    await db.commit()
    assert moderation == (ModerationState.hidden if expected == 200 else ModerationState.ok)


async def test_hide_effects(client: AsyncClient, db: AsyncSession) -> None:
    """Скрытое: пропадает из каталога, остальным — 404, владельцу — карточка с причиной
    (без пожаловавшихся), взаимодействие — 409; открытые жалобы закрываются как resolved;
    действие — в журнале."""
    owner, a, b, stranger = [await api_user(client, db) for _ in range(4)]
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    ra, rb = await _report(client, a, video.id), await _report(client, b, video.id)

    r = await _hide(client, moderator, video.id, "  реклама казино ")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "video_id": str(video.id),
        "moderation": "hidden",
        "moderation_reason": "реклама казино",
        "closed_reports": 2,
    }

    for rid in (ra, rb):
        report = await _get_report(db, rid)
        assert report.status == ReportStatus.resolved
        assert report.resolved_by == uuid.UUID(moderator["id"])
        assert report.resolved_at is not None
    assert not {ra, rb} & set(await _queue_ids(client, moderator))
    assert {ra, rb} <= set(await _queue_ids(client, moderator, status="resolved"))

    # Каталог и чужие.
    feed = (await client.get("/api/videos")).json()["items"]
    assert str(video.id) not in {i["id"] for i in feed}
    search = (await client.get("/api/videos", params={"q": video.title})).json()["items"]
    assert str(video.id) not in {i["id"] for i in search}
    channel = (await client.get(f"/api/channels/{owner['username']}/videos")).json()["items"]
    assert str(video.id) not in {i["id"] for i in channel}
    for viewer in (None, stranger):
        card = await client.get(f"/api/videos/{video.id}", headers=viewer["auth"] if viewer else {})
        assert card.status_code == 404, card.text

    # Владелец видит причину, но не пожаловавшихся.
    card = await client.get(f"/api/videos/{video.id}", headers=owner["auth"])
    assert card.status_code == 200, card.text
    assert card.json()["state"]["moderation"] == "hidden"
    assert card.json()["state"]["moderation_reason"] == "реклама казино"
    for secret in (a["username"], b["username"], a["id"], b["id"], ra, rb):
        assert secret not in card.text
    react = await client.put(
        f"/api/videos/{video.id}/reaction", json={"kind": "like"}, headers=owner["auth"]
    )
    assert react.status_code == 409
    assert react.json()["detail"] == videos_service.HIDDEN_NO_INTERACTION

    [entry] = await _log(db, video.id)
    assert entry.action == ModerationAction.video_hide
    assert entry.actor_id == uuid.UUID(moderator["id"])
    assert entry.reason == "реклама казино"
    assert entry.details == {
        "title": video.title,
        "channel_handle": owner["username"],
        "closed_reports": 2,
        "own_video": False,
    }


async def test_unhide_keeps_reports_closed(client: AsyncClient, db: AsyncSession) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    admin = await api_user(client, db, UserRole.admin)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    assert (await _hide(client, admin, video.id)).status_code == 200

    r = await client.put(_mod_url(video.id), json={"state": "ok"}, headers=admin["auth"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "video_id": str(video.id),
        "moderation": "ok",
        "moderation_reason": None,
        "closed_reports": 0,
    }
    assert (await _get_report(db, rid)).status == ReportStatus.resolved  # не открывается
    feed = (await client.get("/api/videos")).json()["items"]
    assert str(video.id) in {i["id"] for i in feed}
    assert (await client.get(f"/api/videos/{video.id}")).status_code == 200
    assert [e.action for e in await _log(db, video.id)] == [
        ModerationAction.video_hide,
        ModerationAction.video_unhide,
    ]


async def test_repeat_same_state_is_noop(client: AsyncClient, db: AsyncSession) -> None:
    """Повтор того же состояния — 200 без изменений и без записи в журнал; новая причина —
    изменение (запись есть)."""
    owner = await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    ok = await client.put(_mod_url(video.id), json={"state": "ok"}, headers=moderator["auth"])
    assert ok.status_code == 200, ok.text
    assert await _log(db, video.id) == []

    assert (await _hide(client, moderator, video.id, "спам")).status_code == 200
    again = await _hide(client, moderator, video.id, "спам")
    assert again.status_code == 200 and again.json()["closed_reports"] == 0
    assert len(await _log(db, video.id)) == 1
    other = await _hide(client, moderator, video.id, "насилие")
    assert other.json()["moderation_reason"] == "насилие"
    assert len(await _log(db, video.id)) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "hidden"},
        {"state": "hidden", "reason": "   "},
        {"state": "hidden", "reason": "a" * 501},
        {"state": "hidden", "reason": "спам\x00"},
        {"state": "deleted", "reason": "x"},
        {"state": "hidden", "reason": "x", "video_id": str(uuid.uuid4())},
    ],
    ids=["no-reason", "blank-reason", "too-long", "nul", "bad-state", "extra-field"],
)
async def test_hide_invalid_payload_is_422(
    client: AsyncClient, db: AsyncSession, payload: dict
) -> None:
    owner = await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.put(_mod_url(video.id), json=payload, headers=moderator["auth"])
    assert r.status_code == 422, r.text


async def test_hide_missing_video_is_404(client: AsyncClient, db: AsyncSession) -> None:
    moderator = await api_user(client, db, UserRole.moderator)
    r = await _hide(client, moderator, uuid.uuid4())
    assert r.status_code == 404, r.text


async def test_hide_private_video(client: AsyncClient, db: AsyncSession) -> None:
    """Staff видит любое видео — скрыть можно и приватное."""
    owner = await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    assert (await _hide(client, moderator, video.id)).status_code == 200


async def test_owner_deletes_hidden_video(client: AsyncClient, db: AsyncSession) -> None:
    """Владельцу разрешено удалить скрытое видео (решение 2026-11-09); жалобы остаются со
    снимком, видео в них — NULL."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    assert (await _hide(client, moderator, video.id)).status_code == 200

    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text
    report = await _get_report(db, rid)
    assert report.video_id is None and report.video_title == video.title
    assert [e.action for e in await _log(db, video.id)] == [ModerationAction.video_hide]


# --- Порядок блокировок «видео → жалобы» -----------------------------------------------------


async def test_hide_waits_for_report_and_closes_it(client: AsyncClient, db: AsyncSession) -> None:
    """Обратный тест порядка: жалоба держит строку видео (FOR SHARE) и ещё не закоммичена;
    скрытие ждёт её и после commit закрывает и эту жалобу — открытой жалобы на скрытом
    видео не остаётся."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    channel = await channel_of(db, owner)
    reporter_id = uuid.UUID(reporter["id"])

    async def report(session: AsyncSession) -> None:
        reporter_user = await session.get(User, reporter_id)
        where = policies.visible_filter(reporter_user)
        assert await reports_repo.share_lock_video(session, video.id, where) is not None
        created = await reports_repo.create_open(
            session,
            video_id=video.id,
            reporter_id=reporter_id,
            category=ReportCategory.spam,
            comment=None,
            video_title=video.title,
            channel_handle=channel.handle,
            channel_id=channel.id,
        )
        assert created is not None
        await session.commit()

    [r] = await waits_for(report, [lambda: _hide(client, moderator, video.id)])
    assert r.status_code == 200, r.text
    assert r.json()["closed_reports"] == 1
    [row] = list(
        await db.scalars(
            select(Report)
            .where(Report.video_id == video.id)
            .execution_options(populate_existing=True)
        )
    )
    await db.commit()
    assert row.status == ReportStatus.resolved


async def test_dismiss_waits_for_video_deletion(client: AsyncClient, db: AsyncSession) -> None:
    """Удаление видео держит строку видео и каскадом обнуляет video_id жалоб; отклонение
    ждёт строку видео (порядок «видео → жалобы»), без взаимоблокировки, и отклоняет уже
    «осиротевшую» жалобу."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    owner_id = uuid.UUID(owner["id"])

    async def delete_video(session: AsyncSession) -> None:
        me = await session.get(User, owner_id)
        await manage_service.delete_video(session, me, video.id)

    [r] = await waits_for(
        delete_video, [lambda: client.post(_dismiss_url(rid), headers=moderator["auth"])]
    )
    assert r.status_code == 200, r.text
    report = await _get_report(db, rid)
    assert report.status == ReportStatus.dismissed and report.video_id is None


async def test_dismiss_waits_for_hide(client: AsyncClient, db: AsyncSession) -> None:
    """Скрытие держит видео и закрывает жалобы; отклонение той же жалобы ждёт и получает
    409 — жалоба закрыта скрытием (resolved), а не отклонена."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator, admin = (
        await api_user(client, db, UserRole.moderator),
        await api_user(client, db, UserRole.admin),
    )
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    admin_id = uuid.UUID(admin["id"])

    async def hide(session: AsyncSession) -> None:
        me = await session.get(User, admin_id)
        await moderation_service.set_video_moderation(
            session, me, video.id, ModerationState.hidden, "спам"
        )

    [r] = await waits_for(hide, [lambda: client.post(_dismiss_url(rid), headers=moderator["auth"])])
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == moderation_service.REPORT_CLOSED
    assert (await _get_report(db, rid)).status == ReportStatus.resolved


# --- Отклонение жалобы -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "expected"),
    [("anon", 401), ("user", 403), ("video_owner", 403), ("moderator", 200), ("admin", 200)],
)
async def test_dismiss_is_staff_only(
    client: AsyncClient, db: AsyncSession, who: str, expected: int
) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    actor = {
        "anon": None,
        "user": await api_user(client, db),
        "video_owner": owner,
        "moderator": await api_user(client, db, UserRole.moderator),
        "admin": await api_user(client, db, UserRole.admin),
    }[who]
    r = await client.post(_dismiss_url(rid), headers=actor["auth"] if actor else {})
    assert r.status_code == expected, r.text
    status = (await _get_report(db, rid)).status
    assert status == (ReportStatus.dismissed if expected == 200 else ReportStatus.open)


async def test_dismiss(client: AsyncClient, db: AsyncSession) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)

    r = await client.post(
        _dismiss_url(rid), json={"reason": "не нарушение"}, headers=moderator["auth"]
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == rid and body["status"] == "dismissed"
    assert body["resolved_by"] == moderator["id"] and body["resolved_at"]

    again = await client.post(_dismiss_url(rid), headers=moderator["auth"])
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == moderation_service.REPORT_CLOSED
    missing = await client.post(_dismiss_url(uuid.uuid4()), headers=moderator["auth"])
    assert missing.status_code == 404, missing.text

    [entry] = await _log(db, rid)
    assert entry.action == ModerationAction.report_dismiss
    assert entry.reason == "не нарушение"
    assert entry.details["title"] == video.title
    # Видео не тронуто, пользователь может пожаловаться снова.
    assert (await client.get(f"/api/videos/{video.id}")).status_code == 200
    await _report(client, reporter, video.id)


async def test_dismiss_after_hide_is_409(client: AsyncClient, db: AsyncSession) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    assert (await _hide(client, moderator, video.id)).status_code == 200
    r = await client.post(_dismiss_url(rid), headers=moderator["auth"])
    assert r.status_code == 409, r.text


# --- Удаление комментария staff --------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.moderator, UserRole.admin])
@pytest.mark.parametrize("state", ["public", "private", "hidden"])
async def test_staff_deletes_comment_with_log(
    client: AsyncClient, db: AsyncSession, role: UserRole, state: str
) -> None:
    """Staff удаляет чужой комментарий под чужим видео в любом состоянии (видит всё):
    204, счётчик −1, запись в журнале со снимком текста."""
    owner, author = await api_user(client, db), await api_user(client, db)
    staff = await api_user(client, db, role)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(
        f"/api/videos/{video.id}/comments", json={"body": "оскорбление"}, headers=author["auth"]
    )
    assert r.status_code == 201, r.text
    comment_id = r.json()["id"]
    if state != "public":
        await db.execute(
            update(Video)
            .where(Video.id == video.id)
            .values(
                visibility=PRIV if state == "private" else PUB,
                moderation=HIDDEN if state == "hidden" else ModerationState.ok,
                moderation_reason="нарушение" if state == "hidden" else None,
            )
        )
        await db.commit()

    page = await client.get(f"/api/videos/{video.id}/comments", headers=staff["auth"])
    assert [c["can_delete"] for c in page.json()["items"]] == [True]

    r = await client.delete(f"/api/comments/{comment_id}", headers=staff["auth"])
    assert r.status_code == 204, r.text
    counter = await db.scalar(
        select(Video.comments_count)
        .where(Video.id == video.id)
        .execution_options(populate_existing=True)
    )
    left = await db.scalar(select(func.count()).where(Comment.video_id == video.id))
    await db.commit()
    assert (counter, left) == (0, 0)

    [entry] = await _log(db, comment_id)
    assert entry.action == ModerationAction.comment_delete
    assert entry.actor_id == uuid.UUID(staff["id"])
    assert entry.details == {
        "video_id": str(video.id),
        "author_id": author["id"],
        "body": "оскорбление",
    }


async def test_author_and_owner_deletion_not_logged(client: AsyncClient, db: AsyncSession) -> None:
    """В журнал модерации — только удаления staff; автор и владелец видео удаляют своё."""
    owner, author = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    ids = []
    for _ in range(2):
        r = await client.post(
            f"/api/videos/{video.id}/comments", json={"body": "x"}, headers=author["auth"]
        )
        ids.append(r.json()["id"])
    assert (
        await client.delete(f"/api/comments/{ids[0]}", headers=author["auth"])
    ).status_code == 204
    assert (
        await client.delete(f"/api/comments/{ids[1]}", headers=owner["auth"])
    ).status_code == 204
    for cid in ids:
        assert await _log(db, cid) == []


async def test_moderation_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    """Сессия staff в cookie: без CSRF-токена скрытие и отклонение — 403, состояние не
    меняется; с токеном — проходят."""
    owner, reporter = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    s = uuid.uuid4().hex[:8]
    r = await client.post(
        "/api/auth/register",
        json={"email": f"m_{s}@example.com", "username": f"m_{s}", "password": "pass-pass-1"},
    )
    assert r.status_code == 201, r.text
    await db.execute(
        update(User).where(User.id == uuid.UUID(r.json()["id"])).values(role=UserRole.moderator)
    )
    await db.commit()

    hide = {"state": "hidden", "reason": "спам"}
    assert (await client.put(_mod_url(video.id), json=hide)).status_code == 403
    assert (await client.post(_dismiss_url(rid))).status_code == 403
    assert (await _get_report(db, rid)).status == ReportStatus.open
    assert await _log(db, video.id) == [] and await _log(db, rid) == []

    csrf = {"X-CSRF-Token": client.cookies["csrf"]}
    assert (await client.post(_dismiss_url(rid), headers=csrf)).status_code == 200
    assert (await client.put(_mod_url(video.id), json=hide, headers=csrf)).status_code == 200
    client.cookies.clear()


@pytest.mark.parametrize(
    "payload",
    [{"reason": "a" * 501}, {"reason": "нет\x00"}, {"reason": "x", "status": "resolved"}],
    ids=["too-long", "nul", "extra-field"],
)
async def test_dismiss_invalid_payload_is_422(
    client: AsyncClient, db: AsyncSession, payload: dict
) -> None:
    owner, reporter = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rid = await _report(client, reporter, video.id)
    r = await client.post(_dismiss_url(rid), json=payload, headers=moderator["auth"])
    assert r.status_code == 422, r.text
    assert (await _get_report(db, rid)).status == ReportStatus.open
    assert await _log(db, rid) == []


async def test_unhide_reason_goes_only_to_log(client: AsyncClient, db: AsyncSession) -> None:
    """Причина возврата пишется в журнал, а на видео причина снимается (владелец её не видит)."""
    owner = await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    assert (await _hide(client, moderator, video.id)).status_code == 200
    r = await client.put(
        _mod_url(video.id),
        json={"state": "ok", "reason": "ошибочное скрытие"},
        headers=moderator["auth"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["moderation_reason"] is None
    card = (await client.get(f"/api/videos/{video.id}", headers=owner["auth"])).json()
    assert card["state"]["moderation_reason"] is None
    assert (await _log(db, video.id))[-1].reason == "ошибочное скрытие"


async def test_queue_shows_private_video_to_staff_only(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Жалоба допущенного зрителя на приватное видео: staff видит её в очереди со снимком
    и состоянием видео; остальным очередь закрыта (403)."""
    owner, granted = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    db.add(VideoAccess(video_id=video.id, user_id=uuid.UUID(granted["id"])))
    await db.commit()
    rid = await _report(client, granted, video.id)

    page = await client.get(QUEUE, headers=moderator["auth"])
    [item] = [i for i in page.json()["items"] if i["id"] == rid]
    assert item["video"]["visibility"] == "private"
    assert item["video"]["title"] == video.title
    for viewer in (owner, granted):
        assert (await client.get(QUEUE, headers=viewer["auth"])).status_code == 403


async def test_hidden_processing_video_stays_hidden_after_ready(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Скрыть можно и необработанное видео; когда обработка завершится (status → ready),
    пометка модерации сохраняется — в каталог видео не попадает."""
    owner = await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, owner), PUB, PROC)
    assert (await _hide(client, moderator, video.id)).status_code == 200

    locked = await videos_repo.lock_video(db, video.id)  # как финализация worker-а
    assert locked is not None
    locked.status = READY
    locked.published_at = func.now()
    await db.commit()

    moderation = await db.scalar(
        select(Video.moderation)
        .where(Video.id == video.id)
        .execution_options(populate_existing=True)
    )
    await db.commit()
    assert moderation == ModerationState.hidden
    feed = (await client.get("/api/videos")).json()["items"]
    assert str(video.id) not in {i["id"] for i in feed}


# --- Своё видео: модератору нельзя, админу можно ----------------------------------------------


async def test_moderator_does_not_see_reports_on_own_video(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Модератор не видит жалобы на свои видео ни в одном статусе — иначе узнал бы автора;
    в том числе после удаления видео (по снимку handle). Другой модератор их видит."""
    mod_owner = await api_user(client, db, UserRole.moderator)
    other_mod = await api_user(client, db, UserRole.moderator)
    reporter = await api_user(client, db)
    channel = await channel_of(db, mod_owner)
    kept, deleted, third = [await seed_video(db, channel, PUB) for _ in range(3)]
    r_kept = await _report(client, reporter, kept.id)
    r_deleted = await _report(client, reporter, deleted.id)
    r_third = await _report(client, reporter, third.id)
    # Жалобы во всех статусах: resolved (скрыл другой), dismissed (отклонил другой), open
    # на удалённом видео.
    assert (await _hide(client, other_mod, kept.id)).status_code == 200
    assert (await client.post(_dismiss_url(r_third), headers=other_mod["auth"])).status_code == 200
    assert (
        await client.delete(f"/api/videos/{deleted.id}", headers=mod_owner["auth"])
    ).status_code == 204

    expected = {"resolved": r_kept, "open": r_deleted, "dismissed": r_third}
    for status, rid in expected.items():
        r = await client.get(QUEUE, params={"status": status}, headers=mod_owner["auth"])
        assert r.status_code == 200, r.text
        assert reporter["username"] not in r.text and reporter["id"] not in r.text, status
        assert rid not in r.text, status
        assert rid in await _queue_ids(client, other_mod, status=status), status


async def test_moderator_cannot_moderate_own_video(client: AsyncClient, db: AsyncSession) -> None:
    """Модератор — владелец видео: скрыть, вернуть, отклонить жалобу на своё — 403, состояние
    не меняется, журнал пуст. Ответ не раскрывает автора жалобы."""
    mod_owner = await api_user(client, db, UserRole.moderator)
    other_mod = await api_user(client, db, UserRole.moderator)
    reporter = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, mod_owner), PUB)
    rid = await _report(client, reporter, video.id)

    r = await _hide(client, mod_owner, video.id)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == moderation_service.OWN_VIDEO
    d = await client.post(_dismiss_url(rid), headers=mod_owner["auth"])
    assert d.status_code == 403, d.text
    assert reporter["username"] not in d.text
    assert (await _get_report(db, rid)).status == ReportStatus.open
    assert await _log(db, video.id) == [] and await _log(db, rid) == []

    # Скрыл другой модератор — вернуть своё видео модератор-владелец не может.
    assert (await _hide(client, other_mod, video.id)).status_code == 200
    back = await client.put(_mod_url(video.id), json={"state": "ok"}, headers=mod_owner["auth"])
    assert back.status_code == 403, back.text
    # Жалоба уже закрыта (resolved): 403 раньше 409 — статус жалобы на своё видео не раскрыт.
    closed = await client.post(_dismiss_url(rid), headers=mod_owner["auth"])
    assert closed.status_code == 403, closed.text
    state = await db.scalar(
        select(Video.moderation)
        .where(Video.id == video.id)
        .execution_options(populate_existing=True)
    )
    await db.commit()
    assert state == ModerationState.hidden


async def test_moderator_cannot_dismiss_report_on_own_deleted_video(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Видео удалено (video_id NULL): своё узнаётся по снимку handle — отклонить нельзя."""
    mod_owner = await api_user(client, db, UserRole.moderator)
    reporter = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, mod_owner), PUB)
    rid = await _report(client, reporter, video.id)
    assert (
        await client.delete(f"/api/videos/{video.id}", headers=mod_owner["auth"])
    ).status_code == 204
    r = await client.post(_dismiss_url(rid), headers=mod_owner["auth"])
    assert r.status_code == 403, r.text
    assert (await _get_report(db, rid)).status == ReportStatus.open
    assert await _log(db, rid) == []


async def test_own_video_refused_before_lock(client: AsyncClient, db: AsyncSession) -> None:
    """Отказ модератору по своему видео — до блокировки строки: при занятой строке 403
    приходит сразу, а не после ожидания."""
    mod_owner = await api_user(client, db, UserRole.moderator)
    video = await seed_video(db, await channel_of(db, mod_owner), PUB)
    async with get_sessionmaker()() as holder:
        assert await videos_repo.lock_for_edit(holder, video.id) is not None
        try:
            r = await asyncio.wait_for(_hide(client, mod_owner, video.id), timeout=2)
        finally:
            await holder.rollback()
    assert r.status_code == 403, r.text


async def test_admin_moderates_own_video_with_log(client: AsyncClient, db: AsyncSession) -> None:
    """Админу своё видео модерировать можно: видит жалобы в очереди, скрывает, отклоняет —
    каждое действие в журнале с пометкой own_video."""
    admin = await api_user(client, db, UserRole.admin)
    a, b = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, admin), PUB)
    ra = await _report(client, a, video.id)

    assert ra in await _queue_ids(client, admin)
    d = await client.post(_dismiss_url(ra), headers=admin["auth"])
    assert d.status_code == 200, d.text
    [dismiss_entry] = await _log(db, ra)
    assert dismiss_entry.details["own_video"] is True

    await _report(client, b, video.id)
    r = await _hide(client, admin, video.id)
    assert r.status_code == 200 and r.json()["closed_reports"] == 1, r.text
    [hide_entry] = await _log(db, video.id)
    assert hide_entry.action == ModerationAction.video_hide
    assert hide_entry.actor_id == uuid.UUID(admin["id"])
    assert hide_entry.details["own_video"] is True

    back = await client.put(_mod_url(video.id), json={"state": "ok"}, headers=admin["auth"])
    assert back.status_code == 200, back.text
    unhide_entry = (await _log(db, video.id))[-1]
    assert unhide_entry.action == ModerationAction.video_unhide
    assert unhide_entry.details["own_video"] is True
