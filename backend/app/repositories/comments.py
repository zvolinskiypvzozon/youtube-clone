"""Комментарии. Счётчик videos.comments_count меняет вызывающий в той же транзакции."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, delete, insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Comment, User, Video


@dataclass(frozen=True)
class CommentRow:
    comment: Comment
    author: Channel
    author_banned: bool


async def page(
    db: AsyncSession,
    video_id: uuid.UUID,
    visible: Callable[[ColumnElement[bool]], ColumnElement[bool]],
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[CommentRow]:
    """Комментарии видео с каналом автора, новые сверху, keyset по (created_at, id);
    индекс ix_comments_video_created. `visible(author_banned)` — условие из policies
    (comment_visible_filter): комментарии забаненных видит только staff."""
    stmt = (
        select(Comment, Channel, User.is_banned)
        .join(Channel, Channel.owner_id == Comment.author_id)
        .join(User, User.id == Comment.author_id)
        .where(Comment.video_id == video_id, visible(User.is_banned))
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(tuple_(Comment.created_at, Comment.id) < tuple_(*after))
    return [CommentRow(c, ch, bool(b)) for c, ch, b in (await db.execute(stmt)).all()]


async def create(db: AsyncSession, video_id: uuid.UUID, author_id: uuid.UUID, body: str) -> Comment:
    stmt = (
        insert(Comment).values(video_id=video_id, author_id=author_id, body=body).returning(Comment)
    )
    return (await db.execute(stmt)).scalar_one()


async def get_with_video_owner(
    db: AsyncSession, comment_id: uuid.UUID
) -> tuple[Comment, uuid.UUID] | None:
    """Комментарий и владелец видео — для проверки права на удаление."""
    stmt = (
        select(Comment, Channel.owner_id)
        .join(Video, Video.id == Comment.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .where(Comment.id == comment_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    return (row[0], row[1]) if row else None


async def remove(db: AsyncSession, comment_id: uuid.UUID) -> bool:
    """True — строка удалена этим запросом (а не параллельным): только тогда счётчик −1."""
    stmt = delete(Comment).where(Comment.id == comment_id).returning(Comment.id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def author_banned(db: AsyncSession, author_id: uuid.UUID) -> bool:
    """Читать под блокировкой строки видео (удаление комментария): бан пересчитывает счётчик
    под той же блокировкой, поэтому −1 за комментарий забаненного не нужен."""
    return bool(await db.scalar(select(User.is_banned).where(User.id == author_id)))
