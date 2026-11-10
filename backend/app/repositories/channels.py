import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, User


async def get_by_handle_with_ban(db: AsyncSession, handle: str) -> tuple[Channel, bool] | None:
    """Канал и «владелец забанен» — для policies.can_view_channel. Вызывать только через
    services/videos.viewable_channel: канал забаненного не-staff не видит. handle — citext:
    поиск без учёта регистра."""
    stmt = (
        select(Channel, User.is_banned)
        .join(User, User.id == Channel.owner_id)
        .where(Channel.handle == handle)
    )
    row = (await db.execute(stmt)).one_or_none()
    return (row[0], bool(row[1])) if row else None


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
