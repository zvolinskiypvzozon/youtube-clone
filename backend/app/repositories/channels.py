import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel


async def get_by_handle(db: AsyncSession, handle: str) -> Channel | None:
    # handle — citext: поиск без учёта регистра.
    return await db.scalar(select(Channel).where(Channel.handle == handle))


async def get_by_owner(db: AsyncSession, owner_id: uuid.UUID) -> Channel:
    """Канал пользователя: у каждого он один и создаётся при регистрации."""
    return (await db.execute(select(Channel).where(Channel.owner_id == owner_id))).scalar_one()


async def subscribers_count(db: AsyncSession, channel_id: uuid.UUID) -> int:
    stmt = select(Channel.subscribers_count).where(Channel.id == channel_id)
    return (await db.execute(stmt)).scalar_one()


async def add_subscribers(db: AsyncSession, channel_id: uuid.UUID, delta: int) -> int:
    """Атомарно меняет subscribers_count и возвращает новое значение."""
    stmt = (
        update(Channel)
        .where(Channel.id == channel_id)
        .values(subscribers_count=Channel.subscribers_count + delta)
        .returning(Channel.subscribers_count)
    )
    return (await db.execute(stmt)).scalar_one()
