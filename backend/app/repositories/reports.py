"""Жалобы на видео. Порядок блокировок — «видео → жалобы» (ADR 0006 доп. 6)."""

import uuid

from sqlalchemy import ColumnElement, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Report, ReportCategory, ReportStatus, Video


async def share_lock_video(
    db: AsyncSession, video_id: uuid.UUID, where: ColumnElement[bool]
) -> tuple[Video, Channel] | None:
    """Видео с каналом под FOR SHARE, если выполнено условие `where` (из policies).

    FOR SHARE конфликтует с FOR NO KEY UPDATE / FOR UPDATE (правка, скрытие, удаление):
    жалоба ждёт их commit, и условие перепроверяется по новой версии строки. Так скрытие
    не пропустит жалобу, вставленную между его UPDATE жалоб и commit. populate_existing —
    строка уже прочитана в сессию при проверке прав и должна перечитаться."""
    stmt = (
        select(Video, Channel)
        .join(Channel, Channel.id == Video.channel_id)
        .where(Video.id == video_id, where)
        .with_for_update(of=Video, read=True)
        .execution_options(populate_existing=True)
    )
    row = (await db.execute(stmt)).first()
    return (row[0], row[1]) if row else None


async def has_open(db: AsyncSession, video_id: uuid.UUID, reporter_id: uuid.UUID) -> bool:
    """Есть ли открытая жалоба пользователя на видео — без блокировок (индекс
    uq_reports_open_video_reporter). Только быстрый отказ; окончательно дубль отсекает
    ON CONFLICT в create_open."""
    stmt = select(
        exists().where(
            Report.video_id == video_id,
            Report.reporter_id == reporter_id,
            Report.status == ReportStatus.open,
        )
    )
    return bool(await db.scalar(stmt))


async def create_open(
    db: AsyncSession,
    *,
    video_id: uuid.UUID,
    reporter_id: uuid.UUID,
    category: ReportCategory,
    comment: str | None,
    video_title: str,
    channel_handle: str,
    channel_id: uuid.UUID,
) -> Report | None:
    """Открытая жалоба; None — у пользователя уже есть открытая жалоба на это видео
    (частичный уникальный индекс uq_reports_open_video_reporter, без гонки проверки)."""
    stmt = (
        insert(Report)
        .values(
            video_id=video_id,
            reporter_id=reporter_id,
            category=category,
            comment=comment,
            video_title=video_title,
            channel_handle=channel_handle,
            channel_id=channel_id,
        )
        .on_conflict_do_nothing(
            index_elements=["video_id", "reporter_id"], index_where=text("status = 'open'")
        )
        .returning(Report)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def count_open_by(db: AsyncSession, reporter_id: uuid.UUID) -> int:
    """Открытые жалобы пользователя на существующие видео (потолок; частичный индекс
    ix_reports_reporter_open). Жалоба на удалённое видео (video_id NULL) остаётся открытой до
    решения staff, но место в потолке не занимает (ревью и аудит шага 2)."""
    stmt = (
        select(func.count())
        .select_from(Report)
        .where(
            Report.reporter_id == reporter_id,
            Report.status == ReportStatus.open,
            Report.video_id.is_not(None),
        )
    )
    return await db.scalar(stmt) or 0
