import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import err
from app.schemas.uploads import (
    UploadCompleteIn,
    UploadCreateIn,
    UploadCreateOut,
    UploadPartUrl,
    UploadStatusOut,
)
from app.services import uploads as uploads_service

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
    responses={401: err("Требуется вход"), 403: err("Неверный CSRF-токен")},
)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadCreateOut,
    summary="Начать загрузку: видео в статусе uploading и подписанные ссылки на части",
    responses={
        400: err("Некорректный запрос или файл слишком большой"),
        409: err("Слишком много незавершённых загрузок"),
    },
)
async def start_upload(data: UploadCreateIn, user: CurrentUser, db: DbSession) -> UploadCreateOut:
    started = await uploads_service.start_upload(db, user, data)
    return UploadCreateOut(
        video_id=started.video_id,
        part_size=started.part_size,
        expires_in=started.expires_in,
        parts=[
            UploadPartUrl(number=i, size=size, url=url)
            for i, (size, url) in enumerate(zip(started.sizes, started.urls, strict=True), start=1)
        ],
    )


@router.post(
    "/{video_id}/complete",
    response_model=UploadStatusOut,
    summary="Завершить загрузку: склеить части и поставить видео в обработку",
    responses={
        400: err("Не все части загружены или размер не совпадает"),
        404: err("Загрузка не найдена"),
        409: err("Загрузка уже завершена или отменена"),
    },
)
async def complete_upload(
    video_id: uuid.UUID, data: UploadCompleteIn, user: CurrentUser, db: DbSession
) -> UploadStatusOut:
    parts = [(p.number, p.etag) for p in data.parts]
    video = await uploads_service.complete_upload(db, user, video_id, parts)
    return UploadStatusOut(video_id=video.id, status=video.status)


@router.delete(
    "/{video_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отменить незавершённую загрузку",
    responses={404: err("Загрузка не найдена"), 409: err("Загрузка уже завершена")},
)
async def cancel_upload(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Response:
    await uploads_service.cancel_upload(db, user, video_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
