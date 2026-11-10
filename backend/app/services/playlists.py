"""Плейлисты: создание, правка, удаление, состав, списки канала и «Мои плейлисты».

Права — только через policies (ADR 0006 доп. 7):
  - открыть плейлист — `can_view_playlist` (private — только владелец, staff тоже нет),
    иначе 404;
  - управлять — `can_manage` (владелец): не может открыть → 404, может открыть, но не
    владелец → 403 (как у видео, ADR 0006 доп. 2);
  - состав, `video_count` и превью — `visible_filter(viewer)` ТЕКУЩЕГО зрителя, а не владельца
    плейлиста: иначе зритель увидел бы приватные видео, к которым допуск есть у владельца;
  - страница канала — `listed_playlists_filter()` (только public).

В плейлист попадают только свои видео (решение архитектора 2026-11-09, подтверждено
2026-11-10): чужое видео, которое зритель видит, → 403, не видит → 404.

Порядок блокировок:
  - создание: строка канала FOR NO KEY UPDATE → подсчёт → INSERT (лимит плейлистов);
  - добавление: права без блокировок → плейлист FOR NO KEY UPDATE (позиция и лимит) → видео
    FOR KEY SHARE с условием «видео этого канала» → INSERT. Удаление видео (задачи → видео →
    каскад элементов) строк плейлистов не блокирует, поэтому цикла нет;
  - удаление элемента: плейлист FOR NO KEY UPDATE → DELETE элемента.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound
from app.models import Channel, Playlist, User
from app.repositories import channels as channels_repo
from app.repositories import playlists as playlists_repo
from app.schemas.playlists import (
    PlaylistCreateIn,
    PlaylistItemOut,
    PlaylistListItem,
    PlaylistOut,
    PlaylistPage,
    PlaylistUpdateIn,
    PlaylistVideoItem,
    PlaylistVideoPage,
)
from app.services import cursor, policies
from app.services import videos as videos_service
from app.services.videos import CHANNEL_NOT_FOUND, VIDEO_NOT_FOUND

PLAYLIST_NOT_FOUND = "Плейлист не найден"
NOT_OWNER = "Изменять плейлист может только его владелец"
NOT_OWN_VIDEO = "В плейлист можно добавить только своё видео"
PLAYLISTS_LIMIT = 200
ITEMS_LIMIT = 5000


def playlists_limit_reached() -> str:
    return f"У канала уже {PLAYLISTS_LIMIT} плейлистов — это максимум"


def items_limit_reached() -> str:
    return f"В плейлисте уже {ITEMS_LIMIT} видео — это максимум"


@dataclass(frozen=True)
class ViewablePlaylist:
    playlist: Playlist
    channel: Channel


async def get_viewable(
    db: AsyncSession, viewer: User | None, playlist_id: uuid.UUID
) -> ViewablePlaylist:
    """Единственная точка входа к одному плейлисту: нет плейлиста или нет прав → 404."""
    loaded = await playlists_repo.get_with_channel(db, playlist_id)
    if loaded is None:
        raise NotFound(PLAYLIST_NOT_FOUND)
    playlist, channel = loaded
    if not policies.can_view_playlist(viewer, channel.owner_id, playlist.visibility):
        raise NotFound(PLAYLIST_NOT_FOUND)
    return ViewablePlaylist(playlist, channel)


async def check_manageable(
    db: AsyncSession, viewer: User, playlist_id: uuid.UUID
) -> ViewablePlaylist:
    """Свой плейлист без блокировки. Отказ — 404 (не может открыть) или 403."""
    viewable = await get_viewable(db, viewer, playlist_id)
    if not policies.can_manage(viewer, viewable.channel.owner_id):
        raise Forbidden(NOT_OWNER)
    return viewable


async def _lock(db: AsyncSession, playlist_id: uuid.UUID) -> Playlist:
    """Блокировка — только после проверки прав: чужой запрос не встаёт в очередь на строку."""
    playlist = await playlists_repo.lock_playlist(db, playlist_id)
    if playlist is None:  # удалён между проверкой и блокировкой
        raise NotFound(PLAYLIST_NOT_FOUND)
    return playlist


# --- Карточка и состав ----------------------------------------------------------------------


async def _out(db: AsyncSession, viewer: User | None, v: ViewablePlaylist) -> PlaylistOut:
    p = v.playlist
    summary = (await playlists_repo.summaries(db, [p.id], policies.visible_filter(viewer)))[p.id]
    thumbs = [summary.thumbnail] if summary.thumbnail is not None else []
    thumbnail_url = (await videos_service.sign_objects(thumbs) or [None])[0]
    owner = policies.can_manage(viewer, v.channel.owner_id)
    return PlaylistOut(
        id=p.id,
        title=p.title,
        description=p.description,
        channel=videos_service.channel_brief(v.channel),
        video_count=summary.video_count,
        thumbnail_url=thumbnail_url,
        visibility=p.visibility if owner else None,
        can_manage=owner,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


async def card(db: AsyncSession, viewer: User | None, playlist_id: uuid.UUID) -> PlaylistOut:
    return await _out(db, viewer, await get_viewable(db, viewer, playlist_id))


async def videos(
    db: AsyncSession,
    viewer: User | None,
    playlist_id: uuid.UUID,
    after_cursor: str | None,
    limit: int,
) -> PlaylistVideoPage:
    """Состав по порядку: только видео, которые может открыть ТЕКУЩИЙ зритель. Недоступное
    видео не показывается совсем (без заглушки), чтобы не раскрывать его существование."""
    await get_viewable(db, viewer, playlist_id)
    after = cursor.decode_position(after_cursor) if after_cursor else None
    rows = await playlists_repo.items_page(
        db, playlist_id, policies.visible_filter(viewer), after, limit + 1
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    signed = iter(
        await videos_service.sign_objects([r.thumbnail for r in rows if r.thumbnail is not None])
    )
    items = [
        PlaylistVideoItem(
            position=r.position,
            id=r.video.id,
            title=r.video.title,
            channel=videos_service.channel_brief(r.channel),
            duration_seconds=r.video.duration_seconds,
            views_count=r.video.views_count,
            published_at=r.video.published_at,
            thumbnail_url=next(signed) if r.thumbnail is not None else None,
            state=(
                videos_service.video_state(r.video)
                if policies.sees_internal_state(viewer, r.channel.owner_id)
                else None
            ),
        )
        for r in rows
    ]
    next_cursor = cursor.encode_position(rows[-1].position) if has_more and rows else None
    return PlaylistVideoPage(items=items, next_cursor=next_cursor)


# --- Управление -----------------------------------------------------------------------------


async def create(db: AsyncSession, viewer: User, data: PlaylistCreateIn) -> PlaylistOut:
    """Лимит плейлистов считается под блокировкой строки канала: иначе два параллельных
    создания на границе лимита оба увидели бы 199."""
    channel = await channels_repo.get_by_owner(db, viewer.id)
    await playlists_repo.lock_channel(db, channel.id)
    if await playlists_repo.count_of_channel(db, channel.id) >= PLAYLISTS_LIMIT:
        raise Conflict(playlists_limit_reached())
    playlist = await playlists_repo.create(
        db, channel.id, data.title, data.description, data.visibility
    )
    await db.commit()
    return await _out(db, viewer, ViewablePlaylist(playlist, channel))


async def update(
    db: AsyncSession, viewer: User, playlist_id: uuid.UUID, data: PlaylistUpdateIn
) -> PlaylistOut:
    channel = (await check_manageable(db, viewer, playlist_id)).channel
    playlist = await _lock(db, playlist_id)
    for field in data.model_fields_set:
        setattr(playlist, field, getattr(data, field))
    await db.commit()
    return await _out(db, viewer, ViewablePlaylist(playlist, channel))


async def delete(db: AsyncSession, viewer: User, playlist_id: uuid.UUID) -> None:
    """Элементы удаляются каскадом. Видео не затрагиваются."""
    await check_manageable(db, viewer, playlist_id)
    await playlists_repo.delete_playlist(db, playlist_id)
    await db.commit()


async def add_video(
    db: AsyncSession, viewer: User, playlist_id: uuid.UUID, video_id: uuid.UUID
) -> tuple[PlaylistItemOut, bool]:
    """Добавляет своё видео в конец. Возвращает элемент и признак «добавлен» (201) или
    «уже был» (200). Видео в любом статусе: другим оно видно, только пока проходит
    visible_filter."""
    viewable = await check_manageable(db, viewer, playlist_id)
    video = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    if not policies.can_manage(viewer, video.channel.owner_id):
        raise Forbidden(NOT_OWN_VIDEO)

    await _lock(db, playlist_id)
    if not await playlists_repo.lock_channel_video(db, video_id, viewable.channel.id):
        raise NotFound(VIDEO_NOT_FOUND)  # удалено, пока ждали блокировку
    existing = await playlists_repo.get_item(db, playlist_id, video_id)
    if existing is not None:
        out = PlaylistItemOut.model_validate(existing, from_attributes=True)
        await db.commit()  # только снять блокировки
        return out, False
    if await playlists_repo.count_items(db, playlist_id) >= ITEMS_LIMIT:
        raise Conflict(items_limit_reached())
    item = await playlists_repo.add_item(db, playlist_id, video_id)
    if item is None:  # не должно случиться под блокировкой плейлиста
        raise Conflict("Видео уже в плейлисте")
    await playlists_repo.touch(db, playlist_id)
    out = PlaylistItemOut.model_validate(item, from_attributes=True)
    await db.commit()
    return out, True


async def remove_video(
    db: AsyncSession, viewer: User, playlist_id: uuid.UUID, video_id: uuid.UUID
) -> None:
    """Идемпотентно: видео нет в плейлисте — тоже успех. Про само видео ничего не
    проверяется и не раскрывается: плейлист свой, ответ одинаков для любого video_id."""
    await check_manageable(db, viewer, playlist_id)
    await _lock(db, playlist_id)
    if await playlists_repo.remove_item(db, playlist_id, video_id):
        await playlists_repo.touch(db, playlist_id)
    await db.commit()


# --- Списки ---------------------------------------------------------------------------------


async def _page(
    db: AsyncSession,
    viewer: User | None,
    channel_id: uuid.UUID,
    *,
    own: bool,
    after_cursor: str | None,
    limit: int,
    contains_video: uuid.UUID | None = None,
) -> PlaylistPage:
    after = cursor.decode(after_cursor) if after_cursor else None
    where = None if own else policies.listed_playlists_filter()
    rows = await playlists_repo.channel_page(
        db, channel_id, where, after, limit + 1, contains_video=contains_video
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    summaries = await playlists_repo.summaries(
        db, [p.id for p, _ in rows], policies.visible_filter(viewer)
    )
    thumbs = [s.thumbnail for s in summaries.values() if s.thumbnail is not None]
    signed = dict(
        zip(
            thumbs,
            await videos_service.sign_objects(thumbs),
            strict=True,
        )
    )
    items = []
    for p, contains in rows:
        s = summaries[p.id]
        items.append(
            PlaylistListItem(
                id=p.id,
                title=p.title,
                video_count=s.video_count,
                thumbnail_url=signed.get(s.thumbnail) if s.thumbnail is not None else None,
                visibility=p.visibility if own else None,
                contains_video=contains,
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
        )
    last = rows[-1][0] if rows else None
    next_cursor = cursor.encode(last.created_at, last.id) if has_more and last else None
    return PlaylistPage(items=items, next_cursor=next_cursor)


async def channel_playlists(
    db: AsyncSession, viewer: User | None, handle: str, after_cursor: str | None, limit: int
) -> PlaylistPage:
    """Страница канала: только public (listed_playlists_filter), одинаково для любого
    зрителя, включая владельца. Число видео и превью — по видео, видимым зрителю."""
    channel = await channels_repo.get_by_handle(db, handle)
    if channel is None:
        raise NotFound(CHANNEL_NOT_FOUND)
    return await _page(db, viewer, channel.id, own=False, after_cursor=after_cursor, limit=limit)


async def my_playlists(
    db: AsyncSession,
    viewer: User,
    after_cursor: str | None,
    limit: int,
    contains_video: uuid.UUID | None,
) -> PlaylistPage:
    """Все свои плейлисты с видимостью; с video_id — пометка «видео уже в плейлисте»."""
    channel = await channels_repo.get_by_owner(db, viewer.id)
    return await _page(
        db,
        viewer,
        channel.id,
        own=True,
        after_cursor=after_cursor,
        limit=limit,
        contains_video=contains_video,
    )
