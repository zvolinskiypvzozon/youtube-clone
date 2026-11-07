import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserSession

TOUCH_INTERVAL = text("interval '10 minutes'")


async def create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: bytes,
    expires_at: datetime,
    ip: str | None,
    user_agent: str | None,
) -> UserSession:
    session = UserSession(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip=ip,
        user_agent=user_agent[:300] if user_agent else None,
    )
    db.add(session)
    await db.flush()
    return session


async def get_active_user(db: AsyncSession, token_hash: bytes) -> tuple[uuid.UUID, User] | None:
    """Пользователь по хэшу токена, если сессия не истекла. Возвращает (id сессии, пользователь)."""
    stmt = (
        select(UserSession.id, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash, UserSession.expires_at > func.now())
    )
    row = (await db.execute(stmt)).one_or_none()
    return (row[0], row[1]) if row else None


async def touch(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Обновляет last_seen_at не чаще раза в 10 минут — без записи на каждый запрос."""
    await db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.last_seen_at < func.now() - TOUCH_INTERVAL,
        )
        .values(last_seen_at=func.now())
    )


async def delete_by_hash(db: AsyncSession, token_hash: bytes) -> None:
    await db.execute(delete(UserSession).where(UserSession.token_hash == token_hash))


async def delete_expired(db: AsyncSession) -> int:
    result = await db.execute(delete(UserSession).where(UserSession.expires_at < func.now()))
    return result.rowcount or 0


async def delete_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
