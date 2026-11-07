"""Фабрики тестовых данных. Каждая создаёт уникальные значения — тесты независимы."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Channel, User, UserRole, Video, VideoStatus, VideoVisibility


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def create_user(
    db: AsyncSession,
    *,
    email: str | None = None,
    username: str | None = None,
    role: UserRole = UserRole.user,
    with_channel: bool = True,
) -> User:
    s = _suffix()
    user = User(
        email=email or f"user_{s}@example.com",
        username=username or f"user_{s}",
        password_hash="not-a-real-hash",
        role=role,
    )
    db.add(user)
    await db.flush()
    if with_channel:
        db.add(Channel(owner_id=user.id, handle=user.username, title=user.username))
        await db.flush()
    return user


async def create_video(
    db: AsyncSession,
    channel: Channel,
    *,
    title: str = "Тестовое видео",
    visibility: VideoVisibility = VideoVisibility.public,
    status: VideoStatus = VideoStatus.ready,
) -> Video:
    video = Video(
        channel_id=channel.id,
        title=title,
        visibility=visibility,
        status=status,
        # published_at — момент первой публикации: готово и не приватно.
        published_at=(
            datetime.now(UTC)
            if status == VideoStatus.ready and visibility != VideoVisibility.private
            else None
        ),
    )
    db.add(video)
    await db.flush()
    return video
