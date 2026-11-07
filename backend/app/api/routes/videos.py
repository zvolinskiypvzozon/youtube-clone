import uuid

from fastapi import APIRouter, Depends

from app.api.deps import Cursor, DbSession, Limit, OptionalUser, no_store
from app.schemas.common import err
from app.schemas.videos import VideoCardOut, VideoPage, VideoStreamOut
from app.services import videos as videos_service

# Ответы содержат подписанные ссылки и зависят от зрителя — не кэшируются.
router = APIRouter(prefix="/videos", tags=["videos"], dependencies=[Depends(no_store)])


@router.get(
    "",
    response_model=VideoPage,
    summary="Лента главной: готовые публичные видео, новые сверху",
    responses={400: err("Некорректный курсор")},
)
async def feed(db: DbSession, cursor: Cursor = None, limit: Limit = 24) -> VideoPage:
    return await videos_service.feed(db, cursor, limit)


@router.get(
    "/{video_id}",
    response_model=VideoCardOut,
    summary="Карточка видео (по ссылке; приватное — только с правом просмотра)",
    responses={404: err("Видео не найдено или нет доступа")},
)
async def get_video(video_id: uuid.UUID, viewer: OptionalUser, db: DbSession) -> VideoCardOut:
    return await videos_service.card(db, viewer, video_id)


@router.get(
    "/{video_id}/stream",
    response_model=VideoStreamOut,
    summary="Ссылки на поток во всех качествах (presigned, короткий срок действия)",
    responses={
        404: err("Видео не найдено или нет доступа"),
        409: err("Видео ещё не обработано"),
    },
)
async def stream(video_id: uuid.UUID, viewer: OptionalUser, db: DbSession) -> VideoStreamOut:
    return await videos_service.stream(db, viewer, video_id)
