from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel


async def get_by_handle(db: AsyncSession, handle: str) -> Channel | None:
    # handle — citext: поиск без учёта регистра.
    return await db.scalar(select(Channel).where(Channel.handle == handle))
