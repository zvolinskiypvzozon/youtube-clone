"""Жалобы: POST /api/videos/{id}/reports (ADR 0006 доп. 6).

Пожаловаться — can_view (иначе 404), затем can_interact (не готово или уже скрыто — 409);
на своё видео — 400; одна открытая жалоба пользователя на видео — 409 (частичный уникальный
индекс). Снимок (название, канал) переживает удаление видео. Порядок блокировок «видео →
жалобы»: жалоба ждёт скрытие, правку и удаление видео и проверяет условия по новой версии.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import Report, ReportStatus, User, UserRole
from app.repositories import videos as videos_repo
from app.schemas.moderation import ReportIn
from app.schemas.videos import VideoUpdateIn
from app.services import manage as manage_service
from app.services import reports as reports_service
from app.services import videos as videos_service
from tests.helpers import (
    HIDDEN,
    MANAGE_OPENS,
    MANAGE_STATES,
    MANAGE_VIEWERS,
    PRIV,
    PUB,
    api_user,
    auth_headers,
    channel_of,
    manage_matrix,
    seed_video,
    waits_for,
)

NOT_READY_STATES = {"processing", "failed"}
SPAM = {"category": "spam"}


def _url(video_id: uuid.UUID) -> str:
    return f"/api/videos/{video_id}/reports"


async def _reports(db: AsyncSession, **where: object) -> list[Report]:
    stmt = select(Report).filter_by(**where).order_by(Report.created_at)
    rows = list(await db.scalars(stmt))
    await db.commit()
    return rows


async def _count(db: AsyncSession, video_id: uuid.UUID) -> int:
    n = await db.scalar(select(func.count()).where(Report.video_id == video_id))
    await db.commit()
    return n or 0


# --- Создание --------------------------------------------------------------------------------


async def test_create_report_with_snapshot(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    r = await client.post(
        _url(video.id),
        json={"category": "violence", "comment": "  жестокость на 1:20\n"},
        headers=user["auth"],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "category", "status", "created_at"}
    assert body["category"] == "violence"
    assert body["status"] == "open"

    [report] = await _reports(db, video_id=video.id)
    assert str(report.id) == body["id"]
    assert report.reporter_id == uuid.UUID(user["id"])
    assert report.comment == "жестокость на 1:20"  # пробелы по краям обрезаются
    assert report.video_title == video.title
    assert report.channel_handle == owner["username"]
    assert report.resolved_at is None and report.resolved_by is None


@pytest.mark.parametrize("comment", [None, "", "   "], ids=["absent", "empty", "spaces"])
async def test_comment_is_optional(
    client: AsyncClient, db: AsyncSession, comment: str | None
) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    payload = (
        {"category": "other"} if comment is None else {"category": "other", "comment": comment}
    )
    r = await client.post(_url(video.id), json=payload, headers=user["auth"])
    assert r.status_code == 201, r.text
    [report] = await _reports(db, video_id=video.id)
    assert report.comment is None  # пустое пояснение хранится как его отсутствие


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"category": "boring"},
        {"category": "spam", "comment": "a" * 501},
        {"category": "spam", "comment": "плохо\x00"},
        {"category": "spam", "reporter_id": str(uuid.uuid4())},
    ],
    ids=["no-category", "unknown-category", "too-long", "nul", "extra-field"],
)
async def test_invalid_payload_is_422(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    """NUL Postgres в text не принимает: без проверки в схеме был бы 500."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(_url(video.id), json=payload, headers=user["auth"])
    assert r.status_code == 422, r.text
    assert await _count(db, video.id) == 0


async def test_comment_limit_500(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(
        _url(video.id), json={"category": "spam", "comment": "я" * 500}, headers=user["auth"]
    )
    assert r.status_code == 201, r.text


# --- Доступ ----------------------------------------------------------------------------------


@pytest.mark.parametrize("viewer_kind", [*MANAGE_VIEWERS, "owner"])
async def test_access_matrix(client: AsyncClient, db: AsyncSession, viewer_kind: str) -> None:
    """Аноним — 401; не может открыть — 404; своё видео — 400; не готово или уже скрыто —
    409 (для staff тоже: скрытое уже снято с показа); иначе — 201."""
    owner = await api_user(client, db)
    if viewer_kind == "owner":
        videos, _ = await manage_matrix(client, db, owner, "anon")
        viewer: dict | None = owner
        opens = set(MANAGE_STATES)
    else:
        videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
        opens = MANAGE_OPENS.get(viewer_kind, {"public", "unlisted"})

    for name, video in videos.items():
        r = await client.post(_url(video.id), json=SPAM, headers=auth_headers(viewer))
        if viewer is None:
            expected = 401
        elif name not in opens:
            expected = 404
        elif viewer_kind == "owner":
            expected = 400
        elif name in NOT_READY_STATES or name == "hidden":
            expected = 409
        else:
            expected = 201
        assert r.status_code == expected, (name, r.text)
        if expected == 400:
            assert r.json()["detail"] == reports_service.SELF_REPORT
        if name == "hidden" and expected == 409:
            assert r.json()["detail"] == videos_service.HIDDEN_NO_INTERACTION
        assert await _count(db, video.id) == (1 if expected == 201 else 0), name


async def test_not_found_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    hidden = await client.post(_url(private.id), json=SPAM, headers=stranger["auth"])
    missing = await client.post(_url(uuid.uuid4()), json=SPAM, headers=stranger["auth"])
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


async def test_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    """Сессия в cookie: без CSRF-токена — 403 и жалобы нет, с токеном — 201."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    s = uuid.uuid4().hex[:8]
    r = await client.post(
        "/api/auth/register",
        json={"email": f"r_{s}@example.com", "username": f"r_{s}", "password": "pass-pass-1"},
    )
    assert r.status_code == 201, r.text
    assert (await client.post(_url(video.id), json=SPAM)).status_code == 403
    assert await _count(db, video.id) == 0
    csrf = {"X-CSRF-Token": client.cookies["csrf"]}
    r = await client.post(_url(video.id), json=SPAM, headers=csrf)
    assert r.status_code == 201, r.text


async def test_owner_does_not_learn_reporter(client: AsyncClient, db: AsyncSession) -> None:
    """Жалобы видит только staff: в карточке видео и в «Моих видео» владельца нет ни жалоб,
    ни пожаловавшихся."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(
        _url(video.id), json={"category": "spam", "comment": "маркер-жалобы"}, headers=user["auth"]
    )
    assert r.status_code == 201, r.text
    report_id = r.json()["id"]

    for path in (f"/api/videos/{video.id}", "/api/me/videos"):
        page = await client.get(path, headers=owner["auth"])
        assert page.status_code == 200, (path, page.text)
        for secret in (user["id"], user["username"], report_id, "маркер-жалобы", "report"):
            assert secret not in page.text, (path, secret)


# --- Повторы и гонки -------------------------------------------------------------------------


async def test_second_open_report_is_409(client: AsyncClient, db: AsyncSession) -> None:
    owner, user, other = [await api_user(client, db) for _ in range(3)]
    video = await seed_video(db, await channel_of(db, owner), PUB)
    assert (await client.post(_url(video.id), json=SPAM, headers=user["auth"])).status_code == 201

    again = await client.post(_url(video.id), json={"category": "adult"}, headers=user["auth"])
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == reports_service.ALREADY_REPORTED
    # Другой пользователь жалуется независимо.
    assert (await client.post(_url(video.id), json=SPAM, headers=other["auth"])).status_code == 201
    assert await _count(db, video.id) == 2


@pytest.mark.parametrize("closed", [ReportStatus.resolved, ReportStatus.dismissed])
async def test_can_report_again_after_close(
    client: AsyncClient, db: AsyncSession, closed: ReportStatus
) -> None:
    """Уникальность — только среди открытых: после решения по жалобе можно пожаловаться снова."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    assert (await client.post(_url(video.id), json=SPAM, headers=user["auth"])).status_code == 201
    await db.execute(
        update(Report)
        .where(Report.video_id == video.id)
        .values(status=closed, resolved_at=func.now())
    )
    await db.commit()
    assert (await client.post(_url(video.id), json=SPAM, headers=user["auth"])).status_code == 201
    assert await _count(db, video.id) == 2


async def test_concurrent_reports_of_one_user(client: AsyncClient, db: AsyncSession) -> None:
    """10 параллельных жалоб одного пользователя: дубли отсекает индекс, а не проверка
    в коде — ровно одна 201, остальные 409, ни одной 500."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rs = await asyncio.gather(
        *(client.post(_url(video.id), json=SPAM, headers=user["auth"]) for _ in range(10))
    )
    codes = sorted(r.status_code for r in rs)
    assert codes == [201] + [409] * 9, [r.text for r in rs]
    assert await _count(db, video.id) == 1


@pytest.mark.parametrize(
    ("reporter_role", "expected"), [(UserRole.user, 404), (UserRole.moderator, 409)]
)
async def test_rejected_if_hidden_while_waiting(
    client: AsyncClient, db: AsyncSession, reporter_role: UserRole, expected: int
) -> None:
    """Скрытие держит строку видео (FOR NO KEY UPDATE, как будущий эндпоинт модерации):
    жалоба (FOR SHARE) ждёт commit и проверяет условия по новой версии строки. Обычному
    пользователю скрытое уже не видно — 404; модератор видит его, но жаловаться на скрытое
    нельзя — 409. Открытой жалобы на уже скрытое видео не остаётся."""
    owner = await api_user(client, db)
    reporter = await api_user(client, db, reporter_role)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def hide(session: AsyncSession) -> None:
        locked = await videos_repo.lock_for_edit(session, video.id)
        assert locked is not None
        locked.moderation = HIDDEN
        locked.moderation_reason = "нарушение"  # CHECK: причина ⇔ скрыто
        await session.commit()

    [r] = await waits_for(
        hide, [lambda: client.post(_url(video.id), json=SPAM, headers=reporter["auth"])]
    )
    assert r.status_code == expected, r.text
    if expected == 409:
        assert r.json()["detail"] == videos_service.HIDDEN_NO_INTERACTION
    assert await _count(db, video.id) == 0


async def test_rejected_if_made_private_while_waiting(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def make_private(session: AsyncSession) -> object:
        me = await session.get(User, uuid.UUID(owner["id"]))
        return await manage_service.update_video(
            session, me, video.id, VideoUpdateIn(visibility=PRIV)
        )

    [r] = await waits_for(
        make_private, [lambda: client.post(_url(video.id), json=SPAM, headers=stranger["auth"])]
    )
    assert r.status_code == 404, r.text
    assert await _count(db, video.id) == 0


async def test_snapshot_taken_under_lock(client: AsyncClient, db: AsyncSession) -> None:
    """Владелец переименовывает видео, пока жалоба ждёт: в снимке — новое название (строка
    перечитана под блокировкой, а не взята из проверки прав)."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def rename(session: AsyncSession) -> object:
        me = await session.get(User, uuid.UUID(owner["id"]))
        return await manage_service.update_video(
            session, me, video.id, VideoUpdateIn(title="Новое название")
        )

    [r] = await waits_for(
        rename, [lambda: client.post(_url(video.id), json=SPAM, headers=user["auth"])]
    )
    assert r.status_code == 201, r.text
    [report] = await _reports(db, video_id=video.id)
    assert report.video_title == "Новое название"


async def test_rejected_if_video_deleted_while_waiting(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def delete_video(session: AsyncSession) -> None:
        me = await session.get(User, uuid.UUID(owner["id"]))
        await manage_service.delete_video(session, me, video.id)

    [r] = await waits_for(
        delete_video, [lambda: client.post(_url(video.id), json=SPAM, headers=user["auth"])]
    )
    assert r.status_code == 404, r.text
    assert await _reports(db, reporter_id=uuid.UUID(user["id"])) == []


# --- Удаление видео --------------------------------------------------------------------------


async def test_snapshot_survives_video_deletion(client: AsyncClient, db: AsyncSession) -> None:
    """Удаление видео обнуляет video_id (SET NULL); жалобы, открытые и закрытые, остаются
    со снимком — история модерации не пропадает."""
    owner, a, b = [await api_user(client, db) for _ in range(3)]
    video = await seed_video(db, await channel_of(db, owner), PUB)
    title = video.title
    for user in (a, b):
        r = await client.post(_url(video.id), json=SPAM, headers=user["auth"])
        assert r.status_code == 201, r.text
    await db.execute(
        update(Report)
        .where(Report.reporter_id == uuid.UUID(b["id"]))
        .values(status=ReportStatus.dismissed, resolved_at=func.now())
    )
    await db.commit()

    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text

    reports = await _reports(db, video_title=title, channel_handle=owner["username"])
    assert len(reports) == 2
    assert {rep.video_id for rep in reports} == {None}
    assert {rep.status for rep in reports} == {ReportStatus.open, ReportStatus.dismissed}


async def test_repeat_does_not_wait_for_video_lock(client: AsyncClient, db: AsyncSession) -> None:
    """Повторная жалоба отклоняется без блокировки строки видео: пока строку держит правка
    или скрытие, повтор сразу получает 409, а не ждёт (поток повторов не держит FOR SHARE
    и не мешает скрытию — аудит шага 4)."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    assert (await client.post(_url(video.id), json=SPAM, headers=user["auth"])).status_code == 201

    async with get_sessionmaker()() as holder:
        assert await videos_repo.lock_for_edit(holder, video.id) is not None
        try:
            r = await asyncio.wait_for(
                client.post(_url(video.id), json=SPAM, headers=user["auth"]), timeout=2
            )
        finally:
            await holder.rollback()
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == reports_service.ALREADY_REPORTED
    assert await _count(db, video.id) == 1


async def test_busy_if_state_flipped_back(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блокировка с условиями политики не нашла строку (видео на миг стало недоступным),
    а перепроверка видит его снова доступным: 503 с Retry-After, жалобы нет. Окно не
    воспроизводится детерминированно — запрос под блокировкой подменён."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def no_row(*_args: object) -> None:
        return None

    monkeypatch.setattr(reports_service.reports_repo, "share_lock_video", no_row)
    r = await client.post(_url(video.id), json=SPAM, headers=user["auth"])
    assert r.status_code == 503, r.text
    assert r.headers.get("Retry-After")
    assert await _count(db, video.id) == 0


async def test_duplicates_past_precheck_stopped_by_index(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Два запроса одного пользователя прошли быструю проверку (жалобы ещё нет) и ждут
    строку видео: после её освобождения дубль отсекает только ON CONFLICT по частичному
    уникальному индексу — одна 201, вторая 409, не 500."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def hold(session: AsyncSession) -> None:
        assert await videos_repo.lock_for_edit(session, video.id) is not None
        await session.commit()

    def post():
        return client.post(_url(video.id), json=SPAM, headers=user["auth"])

    rs = await waits_for(hold, [post, post])
    assert sorted(r.status_code for r in rs) == [201, 409], [r.text for r in rs]
    assert await _count(db, video.id) == 1


# --- Потолок открытых жалоб (шаг 2 дня 6) -------------------------------------------------


async def test_open_reports_limit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Потолок открытых жалоб пользователя — 409; закрытая жалоба место освобождает."""
    monkeypatch.setattr(reports_service, "OPEN_REPORTS_LIMIT", 2)
    owner, user = await api_user(client, db), await api_user(client, db)
    moderator = await api_user(client, db, UserRole.moderator)
    channel = await channel_of(db, owner)
    v1, v2, v3 = [(await seed_video(db, channel, PUB)).id for _ in range(3)]
    ids = []
    for vid in (v1, v2):
        r = await client.post(_url(vid), json=SPAM, headers=user["auth"])
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    r = await client.post(_url(v3), json=SPAM, headers=user["auth"])
    assert r.status_code == 409
    assert r.json()["detail"] == reports_service.open_reports_limit_reached()
    # Потолок — на пользователя: другой жалуется свободно.
    other = await api_user(client, db)
    assert (await client.post(_url(v3), json=SPAM, headers=other["auth"])).status_code == 201
    # Отклонённая жалоба освобождает место.
    r = await client.post(f"/api/admin/reports/{ids[0]}/dismiss", headers=moderator["auth"])
    assert r.status_code == 200
    assert (await client.post(_url(v3), json=SPAM, headers=user["auth"])).status_code == 201


async def test_open_reports_limit_concurrent(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Две жалобы на разные видео на границе потолка: вторая ждёт первую (строка
    пользователя) и получает 409 — иначе обе увидели бы 0 открытых."""
    monkeypatch.setattr(reports_service, "OPEN_REPORTS_LIMIT", 1)
    owner, user = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    v1, v2 = (await seed_video(db, channel, PUB)).id, (await seed_video(db, channel, PUB)).id

    async def first(session: AsyncSession) -> object:
        me = await session.get(User, uuid.UUID(user["id"]))
        return await reports_service.create_report(session, me, v1, ReportIn(category="spam"))

    [r] = await waits_for(first, [lambda: client.post(_url(v2), json=SPAM, headers=user["auth"])])
    assert r.status_code == 409, r.text
    assert len(await _reports(db, reporter_id=uuid.UUID(user["id"]))) == 1


async def test_reports_on_deleted_videos_do_not_count(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Жалоба на удалённое видео остаётся открытой (её разбирает staff), но место в потолке
    не занимает: иначе автор, удалив видео, лишил бы пожаловавшегося права жаловаться."""
    monkeypatch.setattr(reports_service, "OPEN_REPORTS_LIMIT", 1)
    owner, user = await api_user(client, db), await api_user(client, db)
    channel = await channel_of(db, owner)
    v1, v2 = (await seed_video(db, channel, PUB)).id, (await seed_video(db, channel, PUB)).id
    assert (await client.post(_url(v1), json=SPAM, headers=user["auth"])).status_code == 201
    assert (await client.post(_url(v2), json=SPAM, headers=user["auth"])).status_code == 409
    assert (await client.delete(f"/api/videos/{v1}", headers=owner["auth"])).status_code == 204
    assert (await client.post(_url(v2), json=SPAM, headers=user["auth"])).status_code == 201
    [orphan] = await _reports(db, video_id=None)
    assert orphan.status == ReportStatus.open


async def test_report_waits_for_ban_of_reporter(client: AsyncClient, db: AsyncSession) -> None:
    """Жалоба, пришедшая во время бана её автора, ждёт его (строка пользователя) и получает
    401; жалобы нет."""
    from app.services import bans as bans_service

    owner, user = await api_user(client, db), await api_user(client, db)
    mod = await api_user(client, db, UserRole.moderator)
    video = (await seed_video(db, await channel_of(db, owner), PUB)).id

    async def ban(session: AsyncSession) -> object:
        me = await session.get(User, uuid.UUID(mod["id"]))
        return await bans_service.ban(session, me, uuid.UUID(user["id"]), "спам")

    [r] = await waits_for(ban, [lambda: client.post(_url(video), json=SPAM, headers=user["auth"])])
    assert r.status_code == 401, r.text
    assert await _reports(db, reporter_id=uuid.UUID(user["id"])) == []
