import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel


async def get_by_handle(db: AsyncSession, handle: str) -> Channel | None:
    # handle — citext: поиск без учёта регистра.
    return await db.scalar(select(Channel).where(Channel.handle == handle))


async def get_by_owner(db: AsyncSession, owner_id: uuid.UUID) -> Channel:
    """Канал пользователя: у каждого он один и создаётся при регистрации."""
    return (await db.execute(select(Channel).where(Channel.owner_id == owner_id))).scalar_one()
