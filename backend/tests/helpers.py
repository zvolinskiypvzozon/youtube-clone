"""Общие помощники тестов API: пользователи через API, видео с файлами в хранилище,
ожидание блокировок в тестах гонок."""

import asyncio
import time
import uuid
from datetime import datetime

from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import (
    Channel,
    ModerationState,
    User,
    UserRole,
    Video,
    VideoAccess,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from app.repositories import videos as videos_repo
from tests.factories import create_video

PASSWORD = "test-password-42"


async def new_user(client: AsyncClient) -> dict:
    """Регистрирует пользователя и возвращает id и Bearer-заголовок.

    Bearer, а не cookie: тестам загрузки CSRF не нужен (он проверяется отдельно).
    """
    s = uuid.uuid4().hex[:8]
    creds = {"email": f"u_{s}@example.com", "username": f"u_{s}", "password": PASSWORD}
    r = await client.post("/api/auth/register", json=creds)
    assert r.status_code == 201, r.text
    client.cookies.clear()
    tok = await client.post(
        "/api/auth/token", data={"username": creds["username"], "password": PASSWORD}
    )
    return {
        "id": r.json()["id"],
        "username": creds["username"],
        "auth": {"Authorization": f"Bearer {tok.json()['access_token']}"},
    }


LOCK_WAIT_SECONDS = 5.0


async def backend_pid(session) -> int:
    """pid сеанса Postgres этой сессии (держателя блокировки в тестах гонок)."""
    return await session.scalar(text("SELECT pg_backend_pid()"))


async def wait_for_lock_waiters(blocker_pid: int, count: int = 1) -> None:
    """Ждёт, пока `count` сеансов встанут в ожидание блокировки, которую держит именно
    `blocker_pid` (детерминированные тесты гонок: «второй ждёт первого» проверяется по факту,
    а не паузой; посторонние ожидающие в той же БД не учитываются)."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    async with get_sessionmaker()() as probe:
        while True:
            waiting = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :blocker = ANY(pg_blocking_pids(pid))"
                ),
                {"blocker": blocker_pid},
            )
            await probe.rollback()
            if waiting >= count:
                return
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"ожидающих блокировку {blocker_pid}: {waiting}, нужно {count}"
                )
            await asyncio.sleep(0.02)


async def wait_for_lock_queue(blocker_pid: int, count: int) -> None:
    """Как wait_for_lock_waiters, но считает очередь целиком: второй ожидающий той же строки
    заблокирован первым ожидающим (tuple lock), а не держателем — pg_blocking_pids его
    к держателю не относит. Считаются сеансы, связанные с `blocker_pid` цепочкой."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    async with get_sessionmaker()() as probe:
        while True:
            rows = (
                await probe.execute(
                    text(
                        "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity "
                        "WHERE cardinality(pg_blocking_pids(pid)) > 0"
                    )
                )
            ).all()
            await probe.rollback()
            chain, queued = {blocker_pid}, set()
            grew = True
            while grew:
                grew = False
                for pid, blockers in rows:
                    if pid not in queued and chain.intersection(blockers):
                        queued.add(pid)
                        chain.add(pid)
                        grew = True
            if len(queued) >= count:
                return
            if time.monotonic() > deadline:
                raise AssertionError(f"в очереди за {blocker_pid}: {len(queued)}, нужно {count}")
            await asyncio.sleep(0.02)


# --- Видео: состояния и подготовка данных ------------------------------------------------

PUB, UNL, PRIV = VideoVisibility.public, VideoVisibility.unlisted, VideoVisibility.private
READY, PROC, FAIL = VideoStatus.ready, VideoStatus.processing, VideoStatus.failed
OK, HIDDEN = ModerationState.ok, ModerationState.hidden

MEDIA = {
    VideoFileKind.mp4_720: (b"720" * 4000, "video/mp4", 720),
    VideoFileKind.mp4_360: (b"360" * 3000, "video/mp4", 360),
    VideoFileKind.thumbnail: (b"\xff\xd8thumb", "image/jpeg", None),
}


async def api_user(client: AsyncClient, db: AsyncSession, role: UserRole = UserRole.user) -> dict:
    u = await new_user(client)
    if role != UserRole.user:
        await db.execute(update(User).where(User.id == uuid.UUID(u["id"])).values(role=role))
        await db.commit()
    return u


async def channel_of(db: AsyncSession, user: dict) -> Channel:
    stmt = select(Channel).where(Channel.owner_id == uuid.UUID(user["id"]))
    return (await db.execute(stmt)).scalar_one()


async def put_media(
    db: AsyncSession, video: Video, kinds: tuple[VideoFileKind, ...] = tuple(MEDIA)
) -> None:
    bucket = get_settings().s3_bucket_media
    for kind in kinds:
        body, mime, height = MEDIA[kind]
        key = f"videos/{video.id}/test/{kind.value}"
        await asyncio.to_thread(
            storage.get_s3().put_object, Bucket=bucket, Key=key, Body=body, ContentType=mime
        )
        await videos_repo.add_file(
            db,
            video_id=video.id,
            kind=kind,
            bucket=bucket,
            key=key,
            mime=mime,
            size_bytes=len(body),
            height=height,
        )


async def seed_video(
    db: AsyncSession,
    channel: Channel,
    visibility: VideoVisibility = PUB,
    status: VideoStatus = READY,
    moderation: ModerationState = OK,
    *,
    media: bool = True,
    published_at: datetime | None = None,
) -> Video:
    video = await create_video(db, channel, visibility=visibility, status=status)
    video.moderation = moderation
    if published_at is not None:
        video.published_at = published_at
    if media and status == READY:
        await put_media(db, video)
    await db.commit()
    return video


def auth_headers(user: dict | None) -> dict:
    return user["auth"] if user else {}


# --- Матрица отказов в управлении (ADR 0006, доп. 2) ----------------------------------------

MANAGE_STATES = {
    "public": (PUB, READY, OK),
    "unlisted": (UNL, READY, OK),
    "private": (PRIV, READY, OK),
    "hidden": (PUB, READY, HIDDEN),
    "processing": (PUB, PROC, OK),
    "failed": (PUB, FAIL, OK),
}
# Кто может открыть видео (can_view) — но не управлять: 403. Остальным — 404.
MANAGE_OPENS = {
    "stranger": {"public", "unlisted"},
    "granted": {"public", "unlisted", "private"},
    "moderator": set(MANAGE_STATES),
    "admin": set(MANAGE_STATES),
}
MANAGE_VIEWERS = [*MANAGE_OPENS, "anon"]


async def manage_matrix(
    client: AsyncClient, db: AsyncSession, owner: dict, viewer_kind: str
) -> tuple[dict[str, Video], dict | None]:
    """Видео владельца во всех состояниях MANAGE_STATES и зритель вида `viewer_kind`
    (None — аноним; granted — в списке допуска всех видео)."""
    channel = await channel_of(db, owner)
    videos = {name: await seed_video(db, channel, *state) for name, state in MANAGE_STATES.items()}
    roles = {"moderator": UserRole.moderator, "admin": UserRole.admin}
    viewer = (
        None
        if viewer_kind == "anon"
        else await api_user(client, db, roles.get(viewer_kind, UserRole.user))
    )
    if viewer_kind == "granted":
        db.add_all(
            VideoAccess(video_id=v.id, user_id=uuid.UUID(viewer["id"])) for v in videos.values()
        )
        await db.commit()
    return videos, viewer


def manage_expected(viewer_kind: str, state: str) -> int:
    return 401 if viewer_kind == "anon" else 403 if state in MANAGE_OPENS[viewer_kind] else 404


# --- Планы запросов ------------------------------------------------------------------------


async def generic_plan(db: AsyncSession, stmt, *, no_sort: bool = False) -> str:
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
    if no_sort:
        # «Индекс даёт порядок выдачи»: без этого при статистике почти пустой таблицы
        # (автоанализ после других тестов) planner может предпочесть другой индекс и Sort.
        await raw.execute("SET LOCAL enable_sort = off")
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
