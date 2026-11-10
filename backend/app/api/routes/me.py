from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, Cursor, DbSession, Limit, no_store
from app.schemas.common import err
from app.schemas.videos import MySubscriptionPage, MyVideoPage
from app.services import subscriptions as subscriptions_service
from app.services import videos as videos_service

# Персональные данные и подписанные ссылки — не кэшируются.
router = APIRouter(prefix="/me", tags=["me"], dependencies=[Depends(no_store)])


@router.get(
    "/videos",
    response_model=MyVideoPage,
    summary="Мои видео: все свои видео в любом статусе, новые загрузки сверху",
    responses={400: err("Некорректный курсор"), 401: err("Требуется вход")},
)
async def my_videos(
    user: CurrentUser, db: DbSession, cursor: Cursor = None, limit: Limit = 24
) -> MyVideoPage:
    return await videos_service.my_videos(db, user, cursor, limit)


@router.get(
    "/subscriptions",
    response_model=MySubscriptionPage,
    summary="Мои подписки: каналы, новые подписки сверху",
    description="Только свои: чужие подписки и подписчики канала через API не отдаются.",
    responses={400: err("Некорректный курсор"), 401: err("Требуется вход")},
)
async def my_subscriptions(
    user: CurrentUser, db: DbSession, cursor: Cursor = None, limit: Limit = 24
) -> MySubscriptionPage:
    return await subscriptions_service.my_subscriptions(db, user, cursor, limit)
