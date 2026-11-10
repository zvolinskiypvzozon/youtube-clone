"""Пересчёт денормализованных счётчиков по строкам незабаненных пользователей (ADR 0006
доп. 8): реакции, комментарии и подписки забаненного в счётчиках не учитываются.

Пересчёт полный (COUNT по строкам), а не ±1: так счётчик сходится при любом расхождении.
Строки видео и каналов блокируются одним запросом в порядке id — два бана и любые пути,
меняющие один счётчик (каждый держит одну строку), взаимно не блокируются.
"""

import uuid

from sqlalchemy import and_, func, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Comment, Reaction, ReactionKind, Subscription, User, Video


async def videos_touched_by(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Видео, где у пользователя есть реакция или комментарий, — по возрастанию id."""
    ids = union(
        select(Reaction.video_id).where(Reaction.user_id == user_id),
        select(Comment.video_id).where(Comment.author_id == user_id),
    ).subquery()
    stmt = (
        select(Video.id)
        .where(Video.id.in_(select(ids.c[0])))
        .order_by(Video.id)
        .with_for_update(key_share=True)
    )
    return list(await db.scalars(stmt))


async def channels_subscribed_by(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Каналы, на которые пользователь подписан, — по возрастанию id, FOR NO KEY UPDATE."""
    stmt = (
        select(Channel.id)
        .where(
            Channel.id.in_(select(Subscription.channel_id).where(Subscription.user_id == user_id))
        )
        .order_by(Channel.id)
        .with_for_update(key_share=True)
    )
    return list(await db.scalars(stmt))


def _active(user_id_column) -> object:
    return select(User.id).where(User.id == user_id_column, User.is_banned.is_(False)).exists()


async def recount_videos(db: AsyncSession, video_ids: list[uuid.UUID]) -> None:
    """likes_count, dislikes_count, comments_count — по реакциям и комментариям
    незабаненных. Строки уже заблокированы (videos_touched_by)."""
    if not video_ids:
        return

    def reactions(kind: ReactionKind):
        return (
            select(func.count())
            .select_from(Reaction)
            .where(Reaction.video_id == Video.id, Reaction.kind == kind, _active(Reaction.user_id))
            .scalar_subquery()
        )

    comments = (
        select(func.count())
        .select_from(Comment)
        .where(and_(Comment.video_id == Video.id, _active(Comment.author_id)))
        .scalar_subquery()
    )
    stmt = (
        update(Video)
        .where(Video.id.in_(video_ids))
        .values(
            likes_count=reactions(ReactionKind.like),
            dislikes_count=reactions(ReactionKind.dislike),
            comments_count=comments,
            updated_at=Video.updated_at,  # время правки метаданных, не счётчиков
        )
        .execution_options(synchronize_session=False)
    )
    await db.execute(stmt)


async def recount_channels(db: AsyncSession, channel_ids: list[uuid.UUID]) -> None:
    """subscribers_count — по подпискам незабаненных. Строки уже заблокированы."""
    if not channel_ids:
        return
    subscribers = (
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.channel_id == Channel.id, _active(Subscription.user_id))
        .scalar_subquery()
    )
    stmt = (
        update(Channel)
        .where(Channel.id.in_(channel_ids))
        .values(subscribers_count=subscribers)
        .execution_options(synchronize_session=False)
    )
    await db.execute(stmt)
