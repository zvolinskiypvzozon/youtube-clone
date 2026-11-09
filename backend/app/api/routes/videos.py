import uuid

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentUser, Cursor, DbSession, Limit, OptionalUser, no_store
from app.schemas.common import err
from app.schemas.videos import (
    AccessEntryOut,
    AccessGrantIn,
    AccessListOut,
    ReactionIn,
    ReactionOut,
    VideoCardOut,
    VideoPage,
    VideoStreamOut,
    VideoUpdateIn,
)
from app.services import manage as manage_service
from app.services import reactions as reactions_service
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


@router.patch(
    "/{video_id}",
    response_model=VideoCardOut,
    summary="Изменить название, описание, видимость своего видео",
    responses={
        401: err("Требуется вход"),
        403: err("Видео доступно, но управлять им может только владелец"),
        404: err("Видео не найдено или нет доступа"),
    },
)
async def update_video(
    video_id: uuid.UUID, data: VideoUpdateIn, user: CurrentUser, db: DbSession
) -> VideoCardOut:
    return await manage_service.update_video(db, user, video_id, data)


# Список допуска: права как у правки (ADR 0006, доп. 2) — не может открыть → 404,
# может открыть, но не владелец → 403, аноним → 401.
_MANAGE_ERRORS = {
    401: err("Требуется вход"),
    403: err("Видео доступно, но управлять им может только владелец"),
    404: err("Видео не найдено или нет доступа"),
}


@router.get(
    "/{video_id}/access",
    response_model=AccessListOut,
    summary="Список допуска к своему видео",
    responses=_MANAGE_ERRORS,
)
async def list_access(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AccessListOut:
    return await manage_service.list_access(db, user, video_id)


@router.post(
    "/{video_id}/access",
    response_model=AccessEntryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Открыть своё приватное видео пользователю (по нику)",
    description="201 — допуск выдан, 200 — уже был. Действует, пока видео приватное.",
    responses={
        200: {"model": AccessEntryOut, "description": "Допуск уже был"},
        400: err("Допуск себе не выдаётся: владелец и так видит своё видео"),
        **_MANAGE_ERRORS,
        404: err("Видео или пользователь не найдены"),
        409: err("Достигнут лимит списка допуска"),
    },
)
async def grant_access(
    video_id: uuid.UUID, data: AccessGrantIn, user: CurrentUser, db: DbSession, response: Response
) -> AccessEntryOut:
    entry, created = await manage_service.grant_access(db, user, video_id, data.username)
    if not created:
        response.status_code = status.HTTP_200_OK
    return entry


@router.delete(
    "/{video_id}/access/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отозвать допуск (идемпотентно: записи нет — тоже 204)",
    responses=_MANAGE_ERRORS,
)
async def revoke_access(
    video_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> None:
    await manage_service.revoke_access(db, user, video_id, user_id)


@router.delete(
    "/{video_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить своё видео (в любом статусе) вместе с файлами",
    responses={
        **_MANAGE_ERRORS,
        503: err("Видео сейчас обрабатывается или изменяется — повторить (Retry-After)"),
    },
)
async def delete_video(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await manage_service.delete_video(db, user, video_id)


# Реакции: только тем, кто может видеть видео (can_view, иначе 404), и только готовому.
_REACTION_ERRORS = {
    401: err("Требуется вход"),
    404: err("Видео не найдено или нет доступа"),
    409: err("Видео ещё не обработано"),
    503: err("Видео сейчас изменяется — повторить (Retry-After)"),
}


@router.put(
    "/{video_id}/reaction",
    response_model=ReactionOut,
    summary="Поставить лайк или дизлайк (повтор той же реакции ничего не меняет)",
    responses=_REACTION_ERRORS,
)
async def put_reaction(
    video_id: uuid.UUID, data: ReactionIn, user: CurrentUser, db: DbSession
) -> ReactionOut:
    return await reactions_service.set_reaction(db, user, video_id, data.kind)


@router.delete(
    "/{video_id}/reaction",
    response_model=ReactionOut,
    summary="Снять свою реакцию (идемпотентно)",
    responses=_REACTION_ERRORS,
)
async def delete_reaction(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ReactionOut:
    return await reactions_service.set_reaction(db, user, video_id, None)
