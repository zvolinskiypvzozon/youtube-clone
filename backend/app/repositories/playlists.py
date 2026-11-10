"""Плейлисты и их состав (`playlists`, `playlist_items`).

Условия видимости приходят из services/policies.py параметром `visible`/`where`: репозиторий
сам решений о доступе не принимает. Порядок блокировок — ADR 0006 доп. 7.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    and_,
    delete,
    exists,
    func,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    Channel,
    Playlist,
    PlaylistItem,
    PlaylistVisibility,
    Video,
    VideoFile,
    VideoFileKind,
)


async def get_with_channel(
    db: AsyncSession, playlist_id: uuid.UUID
) -> tuple[Playlist, Channel] | None:
    stmt = (
        select(Playlist, Channel)
        .join(Channel, Channel.id == Playlist.channel_id)
        .where(Playlist.id == playlist_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    return (row[0], row[1]) if row else None


async def lock_channel(db: AsyncSession, channel_id: uuid.UUID) -> None:
    """Строка канала FOR NO KEY UPDATE: сериализует проверку лимита плейлистов. С FOR KEY
    SHARE внешних ключей (вставки видео, подписок, плейлистов) совместима."""
    stmt = select(Channel.id).where(Channel.id == channel_id).with_for_update(key_share=True)
    await db.execute(stmt)


async def count_of_channel(db: AsyncSession, channel_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Playlist).where(Playlist.channel_id == channel_id)
    return await db.scalar(stmt) or 0


async def create(
    db: AsyncSession,
    channel_id: uuid.UUID,
    title: str,
    description: str,
    visibility: PlaylistVisibility,
) -> Playlist:
    playlist = Playlist(
        channel_id=channel_id, title=title, description=description, visibility=visibility
    )
    db.add(playlist)
    await db.flush()
    return playlist


async def lock_playlist(db: AsyncSession, playlist_id: uuid.UUID) -> Playlist | None:
    """FOR NO KEY UPDATE: сериализует добавления (max(position) + 1 и лимит) и правку.
    populate_existing: строка уже прочитана при проверке прав и перечитывается после
    блокировки."""
    stmt = (
        select(Playlist)
        .where(Playlist.id == playlist_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return await db.scalar(stmt)


async def touch(db: AsyncSession, playlist_id: uuid.UUID) -> None:
    """Состав изменился — updated_at плейлиста (вызывать под lock_playlist)."""
    stmt = update(Playlist).where(Playlist.id == playlist_id).values(updated_at=func.now())
    await db.execute(stmt.execution_options(synchronize_session=False))


async def delete_playlist(db: AsyncSession, playlist_id: uuid.UUID) -> None:
    await db.execute(delete(Playlist).where(Playlist.id == playlist_id))


async def lock_channel_video(db: AsyncSession, video_id: uuid.UUID, channel_id: uuid.UUID) -> bool:
    """Видео канала FOR KEY SHARE (после блокировки плейлиста — порядок «плейлист → видео»).

    Удаление видео (FOR UPDATE) ждёт конца добавления, а добавление, ждавшее удаление,
    получает False → 404, а не нарушение внешнего ключа (500). Условие «видео этого канала» —
    в самом запросе, по версии строки после блокировки."""
    stmt = (
        select(Video.id)
        .where(Video.id == video_id, Video.channel_id == channel_id)
        .with_for_update(read=True, key_share=True)
    )
    return await db.scalar(stmt) is not None


async def get_item(
    db: AsyncSession, playlist_id: uuid.UUID, video_id: uuid.UUID
) -> PlaylistItem | None:
    stmt = select(PlaylistItem).where(
        PlaylistItem.playlist_id == playlist_id, PlaylistItem.video_id == video_id
    )
    return await db.scalar(stmt)


async def count_items(db: AsyncSession, playlist_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist_id)
    )
    return await db.scalar(stmt) or 0


async def add_item(
    db: AsyncSession, playlist_id: uuid.UUID, video_id: uuid.UUID
) -> PlaylistItem | None:
    """В конец плейлиста: позиция max + 1 (вызывать под lock_playlist). None — видео уже
    в плейлисте (ON CONFLICT по первичному ключу)."""
    next_position = (
        select(func.coalesce(func.max(PlaylistItem.position), 0) + 1)
        .where(PlaylistItem.playlist_id == playlist_id)
        .scalar_subquery()
    )
    stmt = (
        insert(PlaylistItem)
        .values(playlist_id=playlist_id, video_id=video_id, position=next_position)
        .on_conflict_do_nothing(index_elements=["playlist_id", "video_id"])
        .returning(PlaylistItem.position, PlaylistItem.added_at)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        return None
    return PlaylistItem(
        playlist_id=playlist_id, video_id=video_id, position=row[0], added_at=row[1]
    )


async def remove_item(db: AsyncSession, playlist_id: uuid.UUID, video_id: uuid.UUID) -> bool:
    stmt = delete(PlaylistItem).where(
        PlaylistItem.playlist_id == playlist_id, PlaylistItem.video_id == video_id
    )
    return (await db.execute(stmt)).rowcount > 0


# --- Чтение составов и списков --------------------------------------------------------------


@dataclass(frozen=True)
class ItemRow:
    position: int
    video: Video
    channel: Channel
    thumbnail: tuple[str, str] | None  # (bucket, key)


async def items_page(
    db: AsyncSession,
    playlist_id: uuid.UUID,
    visible: ColumnElement[bool],
    after_position: int | None,
    limit: int,
) -> list[ItemRow]:
    """Состав плейлиста по позиции; `visible` — policies.visible_filter(ТЕКУЩИЙ зритель)."""
    thumb = aliased(VideoFile)
    stmt = (
        select(PlaylistItem.position, Video, Channel, thumb.bucket, thumb.s3_key)
        .join(Video, Video.id == PlaylistItem.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .outerjoin(thumb, and_(thumb.video_id == Video.id, thumb.kind == VideoFileKind.thumbnail))
        .where(PlaylistItem.playlist_id == playlist_id, visible)
        .order_by(PlaylistItem.position)
        .limit(limit)
    )
    if after_position is not None:
        stmt = stmt.where(PlaylistItem.position > after_position)
    rows = (await db.execute(stmt)).all()
    return [
        ItemRow(position, video, channel, (bucket, key) if key else None)
        for position, video, channel, bucket, key in rows
    ]


@dataclass(frozen=True)
class Summary:
    video_count: int
    thumbnail: tuple[str, str] | None  # превью первого видимого видео


async def summaries(
    db: AsyncSession, playlist_ids: list[uuid.UUID], visible: ColumnElement[bool]
) -> dict[uuid.UUID, Summary]:
    """Число видео и превью первого видео — только по видео, видимым текущему зрителю
    (`visible` — policies.visible_filter): иначе счётчик и превью раскрывали бы приватные
    и скрытые видео плейлиста."""
    if not playlist_ids:
        return {}
    counts_stmt = (
        select(PlaylistItem.playlist_id, func.count())
        .join(Video, Video.id == PlaylistItem.video_id)
        .where(PlaylistItem.playlist_id.in_(playlist_ids), visible)
        .group_by(PlaylistItem.playlist_id)
    )
    counts = dict((await db.execute(counts_stmt)).tuples().all())
    thumb = aliased(VideoFile)
    first_stmt = (
        select(PlaylistItem.playlist_id, thumb.bucket, thumb.s3_key)
        .join(Video, Video.id == PlaylistItem.video_id)
        .outerjoin(thumb, and_(thumb.video_id == Video.id, thumb.kind == VideoFileKind.thumbnail))
        .where(PlaylistItem.playlist_id.in_(playlist_ids), visible)
        .order_by(PlaylistItem.playlist_id, PlaylistItem.position)
        .distinct(PlaylistItem.playlist_id)
    )
    firsts = {
        pid: (bucket, key) if key else None
        for pid, bucket, key in (await db.execute(first_stmt)).tuples()
    }
    return {pid: Summary(counts.get(pid, 0), firsts.get(pid)) for pid in playlist_ids}


async def channel_page(
    db: AsyncSession,
    channel_id: uuid.UUID,
    where: ColumnElement[bool] | None,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
    contains_video: uuid.UUID | None = None,
) -> list[tuple[Playlist, bool | None]]:
    """Плейлисты канала, новые сверху, keyset по (created_at, id); ix_playlists_channel_created.
    `where` — policies.listed_playlists_filter() для страницы канала, None — все (свои).
    `contains_video` — пометка «видео уже в плейлисте» (меню «Сохранить в плейлист»)."""
    contains = (
        exists().where(
            PlaylistItem.playlist_id == Playlist.id, PlaylistItem.video_id == contains_video
        )
        if contains_video is not None
        else literal(None)
    )
    stmt = (
        select(Playlist, contains.label("contains"))
        .where(Playlist.channel_id == channel_id)
        .order_by(Playlist.created_at.desc(), Playlist.id.desc())
        .limit(limit)
    )
    if where is not None:
        stmt = stmt.where(where)
    if after is not None:
        stmt = stmt.where(tuple_(Playlist.created_at, Playlist.id) < tuple_(*after))
    return [(row[0], row[1]) for row in await db.execute(stmt)]
