"""Реакции (лайк/дизлайк). Счётчики видео меняет вызывающий под блокировкой строки видео."""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reaction, ReactionKind


async def get_kind(
    db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID
) -> ReactionKind | None:
    stmt = select(Reaction.kind).where(Reaction.video_id == video_id, Reaction.user_id == user_id)
    return await db.scalar(stmt)


async def put(
    db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID, kind: ReactionKind
) -> None:
    stmt = insert(Reaction).values(video_id=video_id, user_id=user_id, kind=kind)
    await db.execute(
        # Смена реакции — новое действие: «Понравившиеся» сортируются по времени реакции.
        stmt.on_conflict_do_update(
            index_elements=["video_id", "user_id"], set_={"kind": kind, "created_at": func.now()}
        )
    )


async def remove(db: AsyncSession, video_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await db.execute(
        delete(Reaction).where(Reaction.video_id == video_id, Reaction.user_id == user_id)
    )
