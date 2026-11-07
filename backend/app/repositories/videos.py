import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    delete,
    exists,
    false,
    func,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Channel,
    Job,
    JobType,
    Video,
    VideoAccess,
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
    """Видео канала «в работе»: загружаются или обрабатываются. Лимит на оба статуса —
    иначе один пользователь мог бы завершать загрузки одну за другой и занять очередь
    единственного worker-а."""
    stmt = select(func.count()).where(
        Video.channel_id == channel_id,
        Video.status.in_([VideoStatus.uploading, VideoStatus.processing]),
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


async def get_for_view(
    db: AsyncSession, video_id: uuid.UUID, viewer_id: uuid.UUID | None
) -> tuple[Video, Channel, bool] | None:
    """Видео, его канал и «зритель в списке допуска» — одним запросом, для политики."""
    has_access = (
        exists().where(VideoAccess.video_id == Video.id, VideoAccess.user_id == viewer_id)
        if viewer_id is not None
        else false()
    )
    stmt = (
        select(Video, Channel, has_access.label("has_access"))
        .join(Channel, Channel.id == Video.channel_id)
        .where(Video.id == video_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    return (row[0], row[1], bool(row[2])) if row else None


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


async def get_original(db: AsyncSession, video_id: uuid.UUID) -> VideoFile | None:
    return await db.scalar(
        select(VideoFile).where(
            VideoFile.video_id == video_id, VideoFile.kind == VideoFileKind.original
        )
    )


async def delete_original(db: AsyncSession, video_id: uuid.UUID) -> None:
    await db.execute(
        delete(VideoFile).where(
            VideoFile.video_id == video_id, VideoFile.kind == VideoFileKind.original
        )
    )


async def lock_video(db: AsyncSession, video_id: uuid.UUID) -> Video | None:
    return await db.scalar(select(Video).where(Video.id == video_id).with_for_update())


async def delete_stale_uploads(
    db: AsyncSession, older_than_hours: int, limit: int = 100
) -> list[tuple[uuid.UUID, str | None]]:
    """Удаляет брошенные загрузки и возвращает (id, upload_id) удалённых.

    Условие status='uploading' в самом DELETE: видео, которое пользователь успел завершить
    (перешло в processing), не удалится, и его исходник в хранилище останется нетронутым —
    вызывающий чистит хранилище только для возвращённых строк, ПОСЛЕ commit."""
    stale = (
        select(Video.id)
        .where(
            Video.status == VideoStatus.uploading,
            Video.created_at < func.now() - func.make_interval(0, 0, 0, 0, older_than_hours),
        )
        .limit(limit)
        .scalar_subquery()
    )
    result = await db.execute(
        delete(Video)
        .where(Video.id.in_(stale), Video.status == VideoStatus.uploading)
        .returning(Video.id, Video.upload_id)
    )
    return [(row.id, row.upload_id) for row in result.all()]


async def referenced_media_prefixes(db: AsyncSession, bucket: str) -> set[str]:
    """Префиксы videos/{id}/{token}/, на которые ссылаются строки video_files."""
    keys = await db.scalars(select(VideoFile.s3_key).where(VideoFile.bucket == bucket))
    return {key.rsplit("/", 1)[0] + "/" for key in keys}


async def original_keys(db: AsyncSession, video_ids: list[uuid.UUID]) -> list[tuple[str, str]]:
    if not video_ids:
        return []
    stmt = select(VideoFile.bucket, VideoFile.s3_key).where(
        VideoFile.video_id.in_(video_ids), VideoFile.kind == VideoFileKind.original
    )
    return [(row.bucket, row.s3_key) for row in (await db.execute(stmt)).all()]


async def delete_originals(db: AsyncSession, video_ids: list[uuid.UUID]) -> None:
    if video_ids:
        await db.execute(
            delete(VideoFile).where(
                VideoFile.video_id.in_(video_ids), VideoFile.kind == VideoFileKind.original
            )
        )


async def files_of(db: AsyncSession, video_id: uuid.UUID) -> list[VideoFile]:
    return list(await db.scalars(select(VideoFile).where(VideoFile.video_id == video_id)))


@dataclass(frozen=True)
class CatalogRow:
    video: Video
    channel: Channel
    thumbnail: tuple[str, str] | None  # (bucket, key)


def catalog_page_stmt(
    where: ColumnElement[bool],
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
    channel_id: uuid.UUID | None = None,
) -> Select:
    """Страница каталога, новые сверху, keyset по (published_at, id).

    Условие `where` — из policies (listed_filter): индекс ix_videos_feed (лента) или
    ix_videos_channel_published (канал) читается в порядке выдачи и останавливается на limit.
    """
    thumb = aliased(VideoFile)
    stmt = (
        select(Video, Channel, thumb.bucket, thumb.s3_key)
        .join(Channel, Channel.id == Video.channel_id)
        .outerjoin(thumb, and_(thumb.video_id == Video.id, thumb.kind == VideoFileKind.thumbnail))
        .where(where, Video.published_at.is_not(None))
        .order_by(Video.published_at.desc(), Video.id.desc())
        .limit(limit)
    )
    if channel_id is not None:
        stmt = stmt.where(Video.channel_id == channel_id)
    if after is not None:
        stmt = stmt.where(tuple_(Video.published_at, Video.id) < tuple_(*after))
    return stmt


async def catalog_page(
    db: AsyncSession,
    where: ColumnElement[bool],
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
    channel_id: uuid.UUID | None = None,
) -> list[CatalogRow]:
    rows = (await db.execute(catalog_page_stmt(where, after, limit, channel_id))).all()
    return [
        CatalogRow(video, channel, (bucket, key) if key else None)
        for video, channel, bucket, key in rows
    ]
