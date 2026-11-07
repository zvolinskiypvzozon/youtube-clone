from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import Cursor, DbSession, Limit, no_store
from app.schemas.common import err
from app.schemas.videos import ChannelOut, VideoPage
from app.services import videos as videos_service

router = APIRouter(prefix="/channels", tags=["channels"], dependencies=[Depends(no_store)])

Handle = Annotated[str, Path(pattern=r"^[A-Za-z0-9_.-]{3,30}$")]


@router.get(
    "/{handle}",
    response_model=ChannelOut,
    summary="Канал по нику",
    responses={404: err("Канал не найден")},
)
async def get_channel(handle: Handle, db: DbSession) -> ChannelOut:
    return await videos_service.get_channel(db, handle)


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
