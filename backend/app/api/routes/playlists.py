import uuid

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentUser, Cursor, DbSession, Limit, OptionalUser, no_store
from app.schemas.common import err
from app.schemas.playlists import (
    PlaylistCreateIn,
    PlaylistItemOut,
    PlaylistOut,
    PlaylistUpdateIn,
    PlaylistVideoPage,
)
from app.services import playlists as playlists_service

# Ответы содержат подписанные ссылки и зависят от зрителя — не кэшируются.
router = APIRouter(prefix="/playlists", tags=["playlists"], dependencies=[Depends(no_store)])

# Отказ в управлении — как у видео (ADR 0006 доп. 2): не может открыть → 404, может → 403.
_MANAGE_ERRORS = {
    401: err("Требуется вход"),
    403: err("Изменять плейлист может только его владелец"),
    404: err("Плейлист не найден или нет доступа"),
}


@router.post(
    "",
    response_model=PlaylistOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать плейлист на своём канале",
    responses={401: err("Требуется вход"), 409: err("Достигнут лимит плейлистов канала")},
)
async def create_playlist(data: PlaylistCreateIn, user: CurrentUser, db: DbSession) -> PlaylistOut:
    return await playlists_service.create(db, user, data)


@router.get(
    "/{playlist_id}",
    response_model=PlaylistOut,
    summary="Плейлист: название, канал, число доступных зрителю видео",
    description="Приватный плейлист видит только владелец.",
    responses={404: err("Плейлист не найден или нет доступа")},
)
async def get_playlist(playlist_id: uuid.UUID, viewer: OptionalUser, db: DbSession) -> PlaylistOut:
    return await playlists_service.card(db, viewer, playlist_id)


@router.get(
    "/{playlist_id}/videos",
    response_model=PlaylistVideoPage,
    summary="Видео плейлиста по порядку — только те, что зритель может открыть",
    responses={400: err("Некорректный курсор"), 404: err("Плейлист не найден или нет доступа")},
)
async def playlist_videos(
    playlist_id: uuid.UUID,
    viewer: OptionalUser,
    db: DbSession,
    cursor: Cursor = None,
    limit: Limit = 24,
) -> PlaylistVideoPage:
    return await playlists_service.videos(db, viewer, playlist_id, cursor, limit)


@router.patch(
    "/{playlist_id}",
    response_model=PlaylistOut,
    summary="Изменить название, описание или видимость своего плейлиста",
    responses=_MANAGE_ERRORS,
)
async def update_playlist(
    playlist_id: uuid.UUID, data: PlaylistUpdateIn, user: CurrentUser, db: DbSession
) -> PlaylistOut:
    return await playlists_service.update(db, user, playlist_id, data)


@router.delete(
    "/{playlist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить свой плейлист (видео не затрагиваются)",
    responses=_MANAGE_ERRORS,
)
async def delete_playlist(playlist_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await playlists_service.delete(db, user, playlist_id)


@router.put(
    "/{playlist_id}/videos/{video_id}",
    response_model=PlaylistItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить своё видео в конец плейлиста",
    description="201 — добавлено, 200 — уже было. Только свои видео.",
    responses={
        200: {"model": PlaylistItemOut, "description": "Видео уже в плейлисте"},
        **_MANAGE_ERRORS,
        403: err("Плейлист или видео — не свои"),
        404: err("Плейлист или видео не найдены или нет доступа"),
        409: err("Достигнут лимит видео в плейлисте"),
    },
)
async def add_video(
    playlist_id: uuid.UUID,
    video_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    response: Response,
) -> PlaylistItemOut:
    item, created = await playlists_service.add_video(db, user, playlist_id, video_id)
    if not created:
        response.status_code = status.HTTP_200_OK
    return item


@router.delete(
    "/{playlist_id}/videos/{video_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Убрать видео из своего плейлиста (идемпотентно)",
    responses=_MANAGE_ERRORS,
)
async def remove_video(
    playlist_id: uuid.UUID, video_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> None:
    await playlists_service.remove_video(db, user, playlist_id, video_id)
