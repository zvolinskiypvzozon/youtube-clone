from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import CurrentUser, Cursor, DbSession, Limit, OptionalUser, no_store
from app.schemas.common import err
from app.schemas.videos import ChannelOut, SubscriptionOut, VideoPage
from app.services import subscriptions as subscriptions_service
from app.services import videos as videos_service

router = APIRouter(prefix="/channels", tags=["channels"], dependencies=[Depends(no_store)])

Handle = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{3,30}$")]


@router.get(
    "/{handle}",
    response_model=ChannelOut,
    summary="Канал по нику",
    responses={404: err("Канал не найден")},
)
async def get_channel(handle: Handle, viewer: OptionalUser, db: DbSession) -> ChannelOut:
    return await videos_service.get_channel(db, viewer, handle)


@router.get(
    "/{handle}/videos",
    response_model=VideoPage,
    summary="Видео канала: готовые публичные, новые сверху",
    responses={400: err("Некорректный курсор"), 404: err("Канал не найден")},
)
async def channel_videos(
    handle: Handle,
    db: DbSession,
    cursor: Cursor = None,
    limit: Limit = 24,
) -> VideoPage:
    return await videos_service.channel_videos(db, handle, cursor, limit)


_SUBSCRIPTION_ERRORS = {401: err("Требуется вход"), 404: err("Канал не найден")}


@router.put(
    "/{handle}/subscription",
    response_model=SubscriptionOut,
    summary="Подписаться на канал (повтор ничего не меняет)",
    responses={400: err("Нельзя подписаться на свой канал"), **_SUBSCRIPTION_ERRORS},
)
async def subscribe(handle: Handle, user: CurrentUser, db: DbSession) -> SubscriptionOut:
    return await subscriptions_service.subscribe(db, user, handle)


@router.delete(
    "/{handle}/subscription",
    response_model=SubscriptionOut,
    summary="Отписаться от канала (идемпотентно)",
    responses=_SUBSCRIPTION_ERRORS,
)
async def unsubscribe(handle: Handle, user: CurrentUser, db: DbSession) -> SubscriptionOut:
    return await subscriptions_service.unsubscribe(db, user, handle)
