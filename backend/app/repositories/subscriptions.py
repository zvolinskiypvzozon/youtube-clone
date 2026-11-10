"""Подписки на каналы. Счётчик channels.subscribers_count меняет вызывающий в той же
транзакции — только если строку вставил или удалил именно этот запрос."""

import uuid
from datetime import datetime

from sqlalchemy import delete, exists, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, Subscription


async def add(db: AsyncSession, channel_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """True — подписка создана этим запросом; False — уже была (в том числе параллельный
    запрос того же пользователя успел раньше: конфликт по PK ждёт его commit)."""
    stmt = (
        insert(Subscription)
        .values(channel_id=channel_id, user_id=user_id)
        .on_conflict_do_nothing(index_elements=["channel_id", "user_id"])
        .returning(Subscription.channel_id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def remove(db: AsyncSession, channel_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """True — подписку удалил этот запрос."""
    stmt = (
        delete(Subscription)
        .where(Subscription.channel_id == channel_id, Subscription.user_id == user_id)
        .returning(Subscription.channel_id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def is_subscribed(db: AsyncSession, channel_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    stmt = select(
        exists().where(Subscription.channel_id == channel_id, Subscription.user_id == user_id)
    )
    return bool(await db.scalar(stmt))


async def page_of_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[tuple[Subscription, Channel]]:
    """Подписки пользователя с каналами, новые сверху, keyset по (created_at, channel_id);
    индекс ix_subscriptions_user_created."""
    stmt = (
        select(Subscription, Channel)
        .join(Channel, Channel.id == Subscription.channel_id)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc(), Subscription.channel_id.desc())
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(tuple_(Subscription.created_at, Subscription.channel_id) < tuple_(*after))
    return [(s, c) for s, c in (await db.execute(stmt)).all()]
