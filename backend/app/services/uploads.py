"""Загрузка видео напрямую в хранилище: S3 multipart + presigned URL (ADR 0004).

API не пропускает через себя байты видео: выдаёт подписанные ссылки на части,
браузер грузит их сам (прогресс — XHR), затем вызывает complete.

Гарантии:
  - ключ объекта задаёт сервер: raw/{video_id}/source — клиент не пишет в чужой объект;
  - длина каждой части входит в подпись ссылки — загрузить больше заявленного размера
    нельзя (хранилище отклоняет часть другой длины);
  - не больше N незавершённых загрузок на пользователя (проверка сериализована блокировкой
    строки канала);
  - complete идемпотентен к сбою «части уже склеены, а commit не прошёл»;
  - cancel удаляет и незавершённую загрузку, и уже склеенный объект.
"""

import asyncio
import math
import uuid
from dataclasses import dataclass

from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.errors import BadRequest, Conflict, NotFound
from app.models import User, Video, VideoFileKind, VideoStatus
from app.repositories import videos as videos_repo
from app.schemas.uploads import UploadCreateIn

TOO_MANY_UPLOADS = (
    "Слишком много видео в загрузке или обработке. Дождитесь окончания обработки "
    "или отмените незавершённую загрузку"
)
TOO_LARGE = "Файл слишком большой"
NOT_FOUND = "Загрузка не найдена"
ALREADY_DONE = "Загрузка уже завершена или отменена"
PARTS_MISMATCH = "Переданы не все части файла или части повреждены"
SIZE_MISMATCH = "Размер загруженного файла не совпадает с заявленным"


@dataclass(frozen=True)
class StartedUpload:
    video_id: uuid.UUID
    part_size: int
    expires_in: int
    sizes: list[int]
    urls: list[str]


def raw_key(video_id: uuid.UUID) -> str:
    return f"raw/{video_id}/source"


def part_sizes(size_bytes: int, part_size: int) -> list[int]:
    """Точные длины частей: все по part_size, последняя — остаток."""
    count = max(1, math.ceil(size_bytes / part_size))
    return [part_size] * (count - 1) + [size_bytes - part_size * (count - 1)]


async def start_upload(db: AsyncSession, user: User, data: UploadCreateIn) -> StartedUpload:
    settings = get_settings()
    if data.size_bytes > settings.max_upload_bytes:
        raise BadRequest(TOO_LARGE)
    channel_id = await videos_repo.channel_id_of(db, user.id)
    if channel_id is None:  # канал создаётся при регистрации; отсутствие — нарушение данных
        raise NotFound("Канал не найден")

    await videos_repo.lock_channel(db, channel_id)
    if await videos_repo.count_active_uploads(db, channel_id) >= settings.max_active_uploads:
        await db.rollback()
        raise Conflict(TOO_MANY_UPLOADS)

    video = await videos_repo.create_uploading(
        db,
        channel_id=channel_id,
        title=data.title,
        description=data.description,
        visibility=data.visibility,
        size_bytes=data.size_bytes,
    )
    bucket, key = settings.s3_bucket_raw, raw_key(video.id)
    sizes = part_sizes(data.size_bytes, settings.upload_part_bytes)
    upload_id = await asyncio.to_thread(storage.create_multipart_upload, bucket, key)
    try:
        # Подпись — до commit: если она упадёт, не останется видео в uploading без ссылок.
        urls = await asyncio.to_thread(
            storage.presign_upload_parts,
            bucket,
            key,
            upload_id,
            sizes,
            settings.upload_url_ttl_seconds,
        )
        video.upload_id = upload_id
        await db.commit()
    except Exception:
        await db.rollback()
        await asyncio.to_thread(storage.abort_multipart_upload, bucket, key, upload_id)
        raise

    return StartedUpload(
        video_id=video.id,
        part_size=settings.upload_part_bytes,
        expires_in=settings.upload_url_ttl_seconds,
        sizes=sizes,
        urls=urls,
    )


async def _owned_uploading(db: AsyncSession, user: User, video_id: uuid.UUID) -> Video:
    video = await videos_repo.get_owned_for_update(db, video_id, user.id)
    if video is None:
        await db.rollback()
        raise NotFound(NOT_FOUND)
    if video.status != VideoStatus.uploading or video.upload_id is None:
        await db.rollback()
        raise Conflict(ALREADY_DONE)
    return video


async def complete_upload(
    db: AsyncSession, user: User, video_id: uuid.UUID, parts: list[tuple[int, str]]
) -> Video:
    """Завершение: склейка частей, сверка размера, затем в ОДНОЙ транзакции —
    статус processing, запись об исходнике и задача обработки. Строка видео заблокирована
    (FOR UPDATE): второй параллельный complete дождётся первого и получит 409."""
    settings = get_settings()
    video = await _owned_uploading(db, user, video_id)

    expected = len(part_sizes(video.size_bytes or 0, settings.upload_part_bytes))
    if sorted(n for n, _ in parts) != list(range(1, expected + 1)):
        await db.rollback()
        raise BadRequest(PARTS_MISMATCH)

    bucket, key, upload_id = settings.s3_bucket_raw, raw_key(video.id), video.upload_id
    try:
        await asyncio.to_thread(
            storage.complete_multipart_upload, bucket, key, upload_id, sorted(parts)
        )
    except ClientError as exc:
        # NoSuchUpload при уже существующем объекте: части склеены прошлым вызовом, но его
        # commit не прошёл (сбой процесса, повтор boto после таймаута). Продолжаем — иначе
        # загрузку нельзя было бы ни завершить, ни корректно отменить.
        already_assembled = storage.error_code(exc) == "NoSuchUpload" and (
            await asyncio.to_thread(storage.object_size_or_none, bucket, key) is not None
        )
        if not already_assembled:
            # Неверный ETag, не загруженная часть. Загрузка остаётся незавершённой:
            # клиент может догрузить и повторить complete.
            await db.rollback()
            raise BadRequest(PARTS_MISMATCH) from exc

    actual = await asyncio.to_thread(storage.object_size, bucket, key)
    if actual != video.size_bytes:
        await asyncio.to_thread(storage.delete_object, bucket, key)
        video.status = VideoStatus.failed
        video.failure_reason = SIZE_MISMATCH
        video.upload_id = None
        await db.commit()
        raise BadRequest(SIZE_MISMATCH)

    video.status = VideoStatus.processing
    video.upload_id = None
    await videos_repo.add_file(
        db,
        video_id=video.id,
        kind=VideoFileKind.original,
        bucket=bucket,
        key=key,
        mime="application/octet-stream",
        size_bytes=actual,
    )
    await videos_repo.enqueue_processing(db, video.id)
    await db.commit()
    return video


async def cancel_upload(db: AsyncSession, user: User, video_id: uuid.UUID) -> None:
    settings = get_settings()
    video = await _owned_uploading(db, user, video_id)
    bucket, key, upload_id = settings.s3_bucket_raw, raw_key(video.id), video.upload_id
    await asyncio.to_thread(storage.abort_multipart_upload, bucket, key, upload_id)
    # Объект мог быть уже склеен (complete упал до commit) — удаляем и его.
    await asyncio.to_thread(storage.delete_object, bucket, key)
    await db.delete(video)
    await db.commit()
