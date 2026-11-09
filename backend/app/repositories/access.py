"""Список допуска к видео (`video_access`)."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, VideoAccess


async def list_entries(db: AsyncSession, video_id: uuid.UUID) -> list[tuple[VideoAccess, str]]:
    """Записи допуска с ником пользователя, новые выдачи сверху."""
    stmt = (
        select(VideoAccess, User.username)
        .join(User, User.id == VideoAccess.user_id)
        .where(VideoAccess.video_id == video_id)
        .order_by(VideoAccess.granted_at.desc(), User.username)
    )
    return [(row[0], row[1]) for row in await db.execute(stmt)]


async def get(db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID) -> VideoAccess | None:
    return await db.get(VideoAccess, (video_id, user_id))


async def count(db: AsyncSession, video_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(VideoAccess).where(VideoAccess.video_id == video_id)
    return await db.scalar(stmt) or 0


async def add(db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID) -> datetime | None:
    """Добавляет запись; None — запись уже была (ON CONFLICT DO NOTHING)."""
    stmt = (
        insert(VideoAccess)
        .values(video_id=video_id, user_id=user_id)
        .on_conflict_do_nothing()
        .returning(VideoAccess.granted_at)
    )
    return await db.scalar(stmt)


async def remove(db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await db.execute(
        delete(VideoAccess).where(VideoAccess.video_id == video_id, VideoAccess.user_id == user_id)
    )
