"""Подписки на каналы.

Каналы публичные: политика доступа к видео здесь не участвует, и подписка не открывает
приватные видео канала (доступ к видео — только policies; подписка в нём не учитывается).
Наружу не отдаётся, кто подписан на канал, — только счётчик; свои подписки видит только
сам пользователь (`GET /api/me/subscriptions`, без параметра пользователя).

Счётчик `channels.subscribers_count` без блокировки строки канала: `INSERT … ON CONFLICT
DO NOTHING RETURNING` / `DELETE … RETURNING` говорят, изменил ли состояние именно этот
запрос (параллельные запросы того же пользователя упорядочивает PK подписки), и только
тогда счётчик меняется атомарным `UPDATE … + delta`. Порядок блокировок «подписка → канал».
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, NotFound
from app.models import Channel, User
from app.repositories import channels as channels_repo
from app.repositories import subscriptions as subscriptions_repo
from app.schemas.videos import (
    ChannelBrief,
    MySubscriptionItem,
    MySubscriptionPage,
    SubscriptionOut,
)
from app.services import cursor
from app.services.videos import CHANNEL_NOT_FOUND

SELF_SUBSCRIPTION = "Нельзя подписаться на свой канал"


async def _channel(db: AsyncSession, handle: str) -> Channel:
    channel = await channels_repo.get_by_handle(db, handle)
    if channel is None:
        raise NotFound(CHANNEL_NOT_FOUND)
    return channel


async def _fresh_count(db: AsyncSession, channel_id: uuid.UUID) -> int:
    """Счётчик новым запросом, а не из строки канала, прочитанной в начале: если INSERT или
    DELETE ждал параллельный запрос того же пользователя, тот уже изменил счётчик (ревью:
    иначе ответ «подписан, подписчиков 0»)."""
    return await channels_repo.subscribers_count(db, channel_id)


async def subscribe(db: AsyncSession, viewer: User, handle: str) -> SubscriptionOut:
    channel = await _channel(db, handle)
    if channel.owner_id == viewer.id:
        raise BadRequest(SELF_SUBSCRIPTION)
    if await subscriptions_repo.add(db, channel.id, viewer.id):
        count = await channels_repo.add_subscribers(db, channel.id, +1)
    else:
        count = await _fresh_count(db, channel.id)
    await db.commit()
    return SubscriptionOut(subscribed=True, subscribers_count=count)


async def unsubscribe(db: AsyncSession, viewer: User, handle: str) -> SubscriptionOut:
    """Идемпотентно: подписки нет (в том числе на свой канал) — тоже 200."""
    channel = await _channel(db, handle)
    if await subscriptions_repo.remove(db, channel.id, viewer.id):
        count = await channels_repo.add_subscribers(db, channel.id, -1)
    else:
        count = await _fresh_count(db, channel.id)
    await db.commit()
    return SubscriptionOut(subscribed=False, subscribers_count=count)


async def my_subscriptions(
    db: AsyncSession, viewer: User, after_cursor: str | None, limit: int
) -> MySubscriptionPage:
    after = cursor.decode(after_cursor) if after_cursor else None
    rows = await subscriptions_repo.page_of_user(db, viewer.id, after, limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        MySubscriptionItem(
            channel=ChannelBrief(id=ch.id, handle=ch.handle, title=ch.title),
            subscribers_count=ch.subscribers_count,
            subscribed_at=sub.created_at,
        )
        for sub, ch in rows
    ]
    last = rows[-1][0] if rows else None
    next_cursor = cursor.encode(last.created_at, last.channel_id) if has_more and last else None
    return MySubscriptionPage(items=items, next_cursor=next_cursor)
