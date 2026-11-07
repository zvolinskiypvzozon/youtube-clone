import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Channel,
    Job,
    JobType,
    Video,
    VideoFile,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)


async def channel_id_of(db: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    return await db.scalar(select(Channel.id).where(Channel.owner_id == user_id))


async def lock_channel(db: AsyncSession, channel_id: uuid.UUID) -> None:
    """Блокирует строку канала до конца транзакции: сериализует проверку лимита
    незавершённых загрузок (иначе параллельные старты все увидят count < лимита)."""
    await db.execute(select(Channel.id).where(Channel.id == channel_id).with_for_update())


async def count_active_uploads(db: AsyncSession, channel_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(
        Video.channel_id == channel_id, Video.status == VideoStatus.uploading
    )
    return await db.scalar(stmt) or 0


async def create_uploading(
    db: AsyncSession,
    *,
    channel_id: uuid.UUID,
    title: str,
    description: str,
    visibility: VideoVisibility,
    size_bytes: int,
) -> Video:
    video = Video(
        channel_id=channel_id,
        title=title,
        description=description,
        visibility=visibility,
        status=VideoStatus.uploading,
        size_bytes=size_bytes,
    )
    db.add(video)
    await db.flush()
    return video


async def get_owned_for_update(
    db: AsyncSession, video_id: uuid.UUID, owner_id: uuid.UUID
) -> Video | None:
    """Видео владельца с блокировкой строки (защита от гонки двух complete/cancel).

    Чужое видео неотличимо от несуществующего — вызывающий отвечает 404.
    """
    stmt = (
        select(Video)
        .join(Channel, Channel.id == Video.channel_id)
        .where(Video.id == video_id, Channel.owner_id == owner_id)
        .with_for_update(of=Video)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def add_file(
    db: AsyncSession,
    *,
    video_id: uuid.UUID,
    kind: VideoFileKind,
    bucket: str,
    key: str,
    mime: str,
    size_bytes: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> VideoFile:
    file = VideoFile(
        video_id=video_id,
        kind=kind,
        bucket=bucket,
        s3_key=key,
        mime=mime,
        size_bytes=size_bytes,
        width=width,
        height=height,
    )
    db.add(file)
    await db.flush()
    return file


async def enqueue_processing(db: AsyncSession, video_id: uuid.UUID) -> Job:
    job = Job(type=JobType.process_video, video_id=video_id)
    db.add(job)
    await db.flush()
    return job
