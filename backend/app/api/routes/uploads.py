import re
import uuid

from fastapi import APIRouter, Request, Response, status

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.errors import BadRequest
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


# host или host:port; IPv6 в квадратных скобках. Иное (пробелы, мусор) — 400, а не 500 из boto.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$")


def public_storage_endpoint(request: Request) -> str:
    """Адрес хранилища, на который подписываются URL для клиента (ADR 0004).

    По умолчанию — тот же origin, что у запроса: nginx проксирует /yt-raw/ в хранилище,
    сохраняя Host (он входит в подпись). Подделанный Host даст подпись только для того,
    кто его прислал, — чужому пользователю такую ссылку не выдать.
    """
    configured = get_settings().s3_public_endpoint
    if configured:
        return configured.rstrip("/")
    host = request.headers.get("host", "")
    if not _HOST_RE.match(host):
        raise BadRequest("Некорректный заголовок Host")
    return f"{request.url.scheme}://{host}"


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
async def start_upload(
    data: UploadCreateIn, request: Request, user: CurrentUser, db: DbSession
) -> UploadCreateOut:
    started = await uploads_service.start_upload(db, user, data, public_storage_endpoint(request))
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
