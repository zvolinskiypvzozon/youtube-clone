"""Модерация: очередь жалоб, закрытие жалоб, журнал действий staff.

Порядок блокировок «видео → жалобы» (ADR 0006 доп. 6): функции, меняющие жалобы видео,
вызываются только после блокировки строки видео (videos_repo.lock_for_edit).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Channel,
    ModerationAction,
    ModerationLogEntry,
    Report,
    ReportStatus,
    User,
    Video,
)


@dataclass(frozen=True)
class QueueRow:
    report: Report
    reporter_name: str | None
    video: Video | None
    owner_id: uuid.UUID | None  # владелец видео (для бана из очереди)
    owner_banned: bool | None


async def queue_page(
    db: AsyncSession,
    visible: ColumnElement[bool],
    status: ReportStatus,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[QueueRow]:
    """Жалобы со статусом `status`, видимые staff-зрителю (`visible` — из policies), новые
    сверху, keyset по (created_at, id); индекс ix_reports_status_created. К жалобе — ник
    пожаловавшегося, текущее видео и его владелец (None — пользователь или видео удалены)."""
    owner = aliased(User)
    stmt = (
        select(Report, User.username, Video, Channel.owner_id, owner.is_banned)
        .outerjoin(User, User.id == Report.reporter_id)
        .outerjoin(Video, Video.id == Report.video_id)
        .outerjoin(Channel, Channel.id == Video.channel_id)
        .outerjoin(owner, owner.id == Channel.owner_id)
        .where(Report.status == status, visible)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(tuple_(Report.created_at, Report.id) < tuple_(*after))
    return [QueueRow(*row) for row in (await db.execute(stmt)).all()]


async def get_report(db: AsyncSession, report_id: uuid.UUID) -> Report | None:
    return await db.get(Report, report_id)


async def video_owner(db: AsyncSession, video_id: uuid.UUID) -> uuid.UUID | None:
    """Владелец видео (без блокировки: владелец не меняется); None — видео нет."""
    stmt = (
        select(Channel.owner_id)
        .join(Video, Video.channel_id == Channel.id)
        .where(Video.id == video_id)
    )
    return await db.scalar(stmt)


async def report_target_owner(db: AsyncSession, report: Report) -> uuid.UUID | None:
    """Владелец видео, на которое жалоба: по видео, а для удалённого видео — по снимку handle
    канала (handle неизменяем). None — канала тоже нет."""
    if report.video_id is not None:
        owner = await video_owner(db, report.video_id)
        if owner is not None:
            return owner
    stmt = select(Channel.owner_id).where(
        func.lower(Channel.handle) == func.lower(report.channel_handle)
    )
    return await db.scalar(stmt)


async def channel_handle(db: AsyncSession, channel_id: uuid.UUID) -> str:
    return (await db.execute(select(Channel.handle).where(Channel.id == channel_id))).scalar_one()


async def resolve_open_reports(db: AsyncSession, video_id: uuid.UUID, actor_id: uuid.UUID) -> int:
    """Закрывает открытые жалобы видео как resolved (видео скрыто). Только под блокировкой
    строки видео. Возвращает число закрытых."""
    stmt = (
        update(Report)
        .where(Report.video_id == video_id, Report.status == ReportStatus.open)
        .values(status=ReportStatus.resolved, resolved_by=actor_id, resolved_at=func.now())
        .returning(Report.id)
    )
    return len((await db.execute(stmt)).all())


async def dismiss_open(
    db: AsyncSession, report_id: uuid.UUID, actor_id: uuid.UUID
) -> Report | None:
    """Отклоняет жалобу, если она ещё открыта; None — уже закрыта (параллельно скрыли видео
    или отклонили). Если у жалобы есть видео — только под блокировкой его строки."""
    stmt = (
        update(Report)
        .where(Report.id == report_id, Report.status == ReportStatus.open)
        .values(status=ReportStatus.dismissed, resolved_by=actor_id, resolved_at=func.now())
        .returning(Report)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def log(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    action: ModerationAction,
    target_id: uuid.UUID,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        insert(ModerationLogEntry).values(
            actor_id=actor_id,
            action=action,
            target_id=target_id,
            reason=reason,
            details=details or {},
        )
    )
