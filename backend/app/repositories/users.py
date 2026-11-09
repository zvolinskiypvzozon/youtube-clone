import uuid

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Channel, User, UserRole


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


async def get(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def list_page(db: AsyncSession, *, limit: int, offset: int) -> tuple[list[User], int]:
    total = await db.scalar(select(func.count()).select_from(User)) or 0
    stmt = select(User).order_by(User.created_at.desc(), User.id.desc()).limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars()), total


# Ключ транзакционной advisory-блокировки для смены ролей (произвольная константа).
ROLE_CHANGE_LOCK_ID = 7_100_001


async def lock_role_changes(db: AsyncSession) -> None:
    """Сериализует смены ролей; блокировка снимается при commit/rollback транзакции."""
    await db.execute(select(func.pg_advisory_xact_lock(ROLE_CHANGE_LOCK_ID)))


async def get_role(db: AsyncSession, user_id: uuid.UUID) -> UserRole | None:
    return await db.scalar(select(User.role).where(User.id == user_id))


async def count_admins(db: AsyncSession) -> int:
    return (
        await db.scalar(select(func.count()).select_from(User).where(User.role == UserRole.admin))
        or 0
    )


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    """citext — без учёта регистра."""
    return await db.scalar(select(User).where(User.username == username))
