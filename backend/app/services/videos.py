"""Просмотр видео: карточка, ссылки на поток, каталог (лента, канал).

Доступ — только через policies (ADR 0006): одно видео — `get_viewable` (can_view, иначе 404),
списки каталога — `listed_filter`. Ссылки на файлы — presigned GET с коротким TTL, выдаются
только после проверки прав (ADR 0004).
"""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound
from app.models import Channel, User, Video, VideoFileKind, VideoStatus
from app.repositories import channels as channels_repo
from app.repositories import reactions as reactions_repo
from app.repositories import subscriptions as subscriptions_repo
from app.repositories import videos as videos_repo
from app.schemas.videos import (
    ChannelBrief,
    ChannelOut,
    MyVideoItem,
    MyVideoPage,
    VideoCardOut,
    VideoListItem,
    VideoPage,
    VideoSourceOut,
    VideoStateOut,
    VideoStreamOut,
)
from app.services import cursor, policies

# Один текст для «нет такого» и «нет прав»: ответ не раскрывает существование видео.
VIDEO_NOT_FOUND = "Видео не найдено"
CHANNEL_NOT_FOUND = "Канал не найден"
NOT_READY = "Видео ещё не обработано"
HIDDEN_NO_INTERACTION = "Видео скрыто модератором: реакции и комментарии недоступны"
OWNER_BANNED_NO_INTERACTION = "Автор видео заблокирован: реакции и комментарии недоступны"


def ensure_interactive(video: Video, *, owner_banned: bool) -> None:
    """policies.can_interact с понятной причиной отказа (409) для тех, кто видео видит."""
    if policies.can_interact(video, owner_banned=owner_banned):
        return
    if video.status != VideoStatus.ready:
        raise Conflict(NOT_READY)
    if owner_banned:  # видит только staff
        raise Conflict(OWNER_BANNED_NO_INTERACTION)
    raise Conflict(HIDDEN_NO_INTERACTION)


# Качества потока, лучшее первым.
QUALITIES = {VideoFileKind.mp4_720: "720p", VideoFileKind.mp4_360: "360p"}


@dataclass(frozen=True)
class Viewable:
    video: Video
    channel: Channel
    owner_banned: bool  # видеть может только staff (can_view это уже проверил)


async def get_viewable(db: AsyncSession, viewer: User | None, video_id: uuid.UUID) -> Viewable:
    """Единственная точка входа к одному видео для зрителя: нет видео или нет прав → 404."""
    loaded = await videos_repo.get_for_view(db, video_id, viewer.id if viewer else None)
    if loaded is None:
        raise NotFound(VIDEO_NOT_FOUND)
    facts = policies.VideoFacts.of(
        loaded.video,
        owner_id=loaded.channel.owner_id,
        viewer_has_access=loaded.has_access,
        owner_banned=loaded.owner_banned,
    )
    if not policies.can_view(viewer, facts):
        raise NotFound(VIDEO_NOT_FOUND)
    return Viewable(loaded.video, loaded.channel, loaded.owner_banned)


async def viewable_channel(
    db: AsyncSession, viewer: User | None, handle: str
) -> tuple[Channel, bool]:
    """Единственная точка входа к каналу по нику: нет канала или владелец забанен (для
    не-staff) → 404 (policies.can_view_channel). Второе значение — «владелец забанен»
    (его видит только staff)."""
    found = await channels_repo.get_by_handle_with_ban(db, handle)
    if found is None:
        raise NotFound(CHANNEL_NOT_FOUND)
    channel, owner_banned = found
    if not policies.can_view_channel(viewer, owner_banned=owner_banned):
        raise NotFound(CHANNEL_NOT_FOUND)
    return channel, owner_banned


async def get_viewable_channel(db: AsyncSession, viewer: User | None, handle: str) -> Channel:
    return (await viewable_channel(db, viewer, handle))[0]


async def sign_objects(objects: list[tuple[str, str]]) -> list[str]:
    if not objects:
        return []
    ttl = get_settings().stream_url_ttl_seconds
    return await asyncio.to_thread(storage.presign_get_many, objects, ttl)


def channel_brief(channel: Channel) -> ChannelBrief:
    return ChannelBrief(id=channel.id, handle=channel.handle, title=channel.title)


def video_state(video: Video) -> VideoStateOut:
    """Служебное состояние — отдавать только при policies.sees_internal_state."""
    return VideoStateOut(
        status=video.status,
        visibility=video.visibility,
        moderation=video.moderation,
        moderation_reason=video.moderation_reason,
        failure_reason=video.failure_reason,
    )


async def card(db: AsyncSession, viewer: User | None, video_id: uuid.UUID) -> VideoCardOut:
    v = await get_viewable(db, viewer, video_id)
    video = v.video
    thumbs = [
        (f.bucket, f.s3_key)
        for f in await videos_repo.files_of(db, video.id)
        if f.kind == VideoFileKind.thumbnail
    ]
    thumbnail_url = (await sign_objects(thumbs) or [None])[0]
    state = None
    if policies.sees_internal_state(viewer, v.channel.owner_id):
        state = video_state(video)
    return VideoCardOut(
        id=video.id,
        title=video.title,
        description=video.description,
        channel=channel_brief(v.channel),
        duration_seconds=video.duration_seconds,
        views_count=video.views_count,
        likes_count=video.likes_count,
        dislikes_count=video.dislikes_count,
        comments_count=video.comments_count,
        published_at=video.published_at,
        thumbnail_url=thumbnail_url,
        my_reaction=(
            await reactions_repo.get_kind(db, video.id, viewer.id) if viewer is not None else None
        ),
        state=state,
    )


async def stream(db: AsyncSession, viewer: User | None, video_id: uuid.UUID) -> VideoStreamOut:
    v = await get_viewable(db, viewer, video_id)
    # Не готовое видео проходит политику только для владельца и staff — им понятная ошибка.
    if v.video.status != VideoStatus.ready:
        raise Conflict(NOT_READY)
    files = {f.kind: f for f in await videos_repo.files_of(db, v.video.id)}
    variants = [files[kind] for kind in QUALITIES if kind in files]
    if not variants:  # ready без транскодов — нарушение данных; не отдаём пустой плеер
        raise Conflict(NOT_READY)
    thumb = files.get(VideoFileKind.thumbnail)
    objects = [(f.bucket, f.s3_key) for f in variants]
    if thumb is not None:
        objects.append((thumb.bucket, thumb.s3_key))
    urls = await sign_objects(objects)
    return VideoStreamOut(
        video_id=v.video.id,
        sources=[
            VideoSourceOut(
                quality=QUALITIES[f.kind],
                height=f.height,
                mime=f.mime,
                size_bytes=f.size_bytes,
                url=url,
            )
            for f, url in zip(variants, urls[: len(variants)], strict=True)
        ],
        thumbnail_url=urls[len(variants)] if thumb is not None else None,
        expires_in=get_settings().stream_url_ttl_seconds,
    )


async def _catalog(
    db: AsyncSession,
    after_cursor: str | None,
    limit: int,
    channel_id: uuid.UUID | None = None,
    query: str | None = None,
) -> VideoPage:
    after = cursor.decode(after_cursor) if after_cursor else None
    rows = await videos_repo.catalog_page(
        db,
        policies.listed_filter(),
        after,
        limit + 1,
        channel_id=channel_id,
        title_contains=query,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    thumbs = [r.thumbnail for r in rows if r.thumbnail is not None]
    signed = iter(await sign_objects(thumbs))
    items = [
        VideoListItem(
            id=r.video.id,
            title=r.video.title,
            channel=channel_brief(r.channel),
            duration_seconds=r.video.duration_seconds,
            views_count=r.video.views_count,
            published_at=r.video.published_at,
            thumbnail_url=next(signed) if r.thumbnail is not None else None,
        )
        for r in rows
    ]
    last = rows[-1].video if rows else None
    next_cursor = (
        cursor.encode(last.published_at, last.id)
        if has_more and last is not None and last.published_at is not None
        else None
    )
    return VideoPage(items=items, next_cursor=next_cursor)


async def feed(
    db: AsyncSession, after_cursor: str | None, limit: int, query: str | None = None
) -> VideoPage:
    """Лента главной и поиск: каталог (listed_filter) одинаков для всех зрителей (K3, K4).
    Поиск — подстрока в названии, новые сверху (упрощение: без ранжирования по
    релевантности — оно несовместимо с keyset-пагинацией)."""
    return await _catalog(db, after_cursor, limit, query=query)


async def get_channel(db: AsyncSession, viewer: User | None, handle: str) -> ChannelOut:
    """Канал: публичные данные и счётчик подписчиков (кто подписан — не отдаётся);
    `subscribed` — только про текущего пользователя, null для анонима."""
    channel = await get_viewable_channel(db, viewer, handle)
    subscribed = (
        await subscriptions_repo.is_subscribed(db, channel.id, viewer.id)
        if viewer is not None
        else None
    )
    return ChannelOut(
        id=channel.id,
        handle=channel.handle,
        title=channel.title,
        description=channel.description,
        subscribers_count=channel.subscribers_count,
        created_at=channel.created_at,
        subscribed=subscribed,
    )


async def channel_videos(
    db: AsyncSession, viewer: User | None, handle: str, after_cursor: str | None, limit: int
) -> VideoPage:
    """Видео канала: тот же каталог — владелец свои приватные видит в «Моих видео». Канал
    забаненного — 404 (для staff — пустой каталог: listed_filter одинаков для всех)."""
    channel = await get_viewable_channel(db, viewer, handle)
    return await _catalog(db, after_cursor, limit, channel_id=channel.id)


async def my_videos(
    db: AsyncSession, viewer: User, after_cursor: str | None, limit: int
) -> MyVideoPage:
    """«Мои видео»: свои видео в любом статусе (policies.owned_filter), новые сверху."""
    after = cursor.decode(after_cursor) if after_cursor else None
    rows = await videos_repo.own_page(db, policies.owned_filter(viewer), after, limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    signed = iter(await sign_objects([thumb for _, thumb in rows if thumb is not None]))
    items = [
        MyVideoItem(
            id=video.id,
            title=video.title,
            duration_seconds=video.duration_seconds,
            views_count=video.views_count,
            created_at=video.created_at,
            published_at=video.published_at,
            thumbnail_url=next(signed) if thumb is not None else None,
            state=video_state(video),
        )
        for video, thumb in rows
    ]
    last = rows[-1][0] if rows else None
    next_cursor = cursor.encode(last.created_at, last.id) if has_more and last else None
    return MyVideoPage(items=items, next_cursor=next_cursor)


async def add_view(db: AsyncSession, viewer: User | None, video_id: uuid.UUID) -> None:
    """Просмотр: плеер (фронтенд, день 7) шлёт один запрос после 5 с воспроизведения.
    Доступ — can_view (иначе 404), не готово — 409. Засчитывается, только если в момент
    записи зритель всё ещё может видеть видео и с ним можно взаимодействовать
    (visible_filter + interactable_filter в самом UPDATE, ADR 0006 доп. 4): просмотр скрытого
    владельцем или staff — разбор, не просмотр (204 без счёта). Дедупликации нет —
    упрощение дня 5, накрутку ограничит rate limit дня 6."""
    viewable = await get_viewable(db, viewer, video_id)
    if viewable.video.status != VideoStatus.ready:
        raise Conflict(NOT_READY)
    where = and_(policies.visible_filter(viewer), policies.interactable_filter())
    await videos_repo.add_view(db, video_id, where)
    await db.commit()
