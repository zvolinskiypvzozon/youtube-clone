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

from app.core.errors import BadRequest, Conflict
from app.models import Channel, User
from app.repositories import channels as channels_repo
from app.repositories import subscriptions as subscriptions_repo
from app.schemas.videos import (
    ChannelBrief,
    MySubscriptionItem,
    MySubscriptionPage,
    SubscriptionOut,
)
from app.services import bans, cursor, policies
from app.services import videos as videos_service

SELF_SUBSCRIPTION = "Нельзя подписаться на свой канал"
OWNER_BANNED_NO_SUBSCRIPTION = "Автор канала заблокирован: подписка недоступна"


async def _channel(db: AsyncSession, viewer: User, handle: str) -> tuple[Channel, bool]:
    """Канал забаненного — 404 и для подписки, и для отписки (одно правило для контента
    забаненного, ADR 0006 доп. 8); staff его видит — второе значение «владелец забанен»."""
    return await videos_service.viewable_channel(db, viewer, handle)


async def _fresh_count(db: AsyncSession, channel_id: uuid.UUID) -> int:
    """Счётчик новым запросом, а не из строки канала, прочитанной в начале: если INSERT или
    DELETE ждал параллельный запрос того же пользователя, тот уже изменил счётчик (ревью:
    иначе ответ «подписан, подписчиков 0»)."""
    return await channels_repo.subscribers_count(db, channel_id)


async def subscribe(db: AsyncSession, viewer: User, handle: str) -> SubscriptionOut:
    channel, owner_banned = await _channel(db, viewer, handle)
    if channel.owner_id == viewer.id:
        raise BadRequest(SELF_SUBSCRIPTION)
    if owner_banned:  # видит только staff; подписка — взаимодействие, как реакция (409)
        raise Conflict(OWNER_BANNED_NO_SUBSCRIPTION)
    await bans.lock_self_active(db, viewer)  # «пользователь → канал», как у бана (доп. 8)
    if await subscriptions_repo.add(db, channel.id, viewer.id):
        count = await channels_repo.add_subscribers(db, channel.id, +1)
    else:
        count = await _fresh_count(db, channel.id)
    await db.commit()
    return SubscriptionOut(subscribed=True, subscribers_count=count)


async def unsubscribe(db: AsyncSession, viewer: User, handle: str) -> SubscriptionOut:
    """Идемпотентно: подписки нет (в том числе на свой канал) — тоже 200."""
    channel, _ = await _channel(db, viewer, handle)  # отписка staff убирает данные — можно
    # И при отписке: −1 после пересчёта бана ушёл бы в минус относительно строк.
    await bans.lock_self_active(db, viewer)
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
    rows = await subscriptions_repo.page_of_user(
        db, viewer.id, policies.channel_visible_filter(viewer), after, limit + 1
    )
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
