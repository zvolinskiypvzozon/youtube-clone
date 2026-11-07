import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Channel, User


async def get_by_login(db: AsyncSession, login: str) -> User | None:
    """Поиск по email (если есть @) или имени. citext — без учёта регистра."""
    column = User.email if "@" in login else User.username
    return (await db.execute(select(User).where(column == login))).scalar_one_or_none()


async def get_with_channel(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    stmt = select(User).where(User.id == user_id).options(selectinload(User.channel))
    return (await db.execute(stmt)).scalar_one_or_none()


async def email_taken(db: AsyncSession, email: str) -> bool:
    return bool(await db.scalar(select(exists().where(User.email == email))))


async def username_taken(db: AsyncSession, username: str) -> bool:
    # handle канала = username, поэтому проверяем оба.
    return bool(
        await db.scalar(
            select(
                exists().where(User.username == username)
                | exists().where(Channel.handle == username)
            )
        )
    )


async def create_with_channel(
    db: AsyncSession, *, email: str, username: str, password_hash: str
) -> User:
    user = User(email=email, username=username, password_hash=password_hash)
    db.add(user)
    await db.flush()
    db.add(Channel(owner_id=user.id, handle=username, title=username))
    await db.flush()
    return user
