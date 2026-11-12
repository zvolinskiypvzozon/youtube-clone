"""Удаление видео: строки в БД одной транзакцией, объекты хранилища — после commit.

Права здесь не проверяются: вызывающий (владелец — services/manage, модерация — день 6)
проверяет их до вызова, без блокировок.

Порядок блокировок «задачи → видео», как у worker-а (`_finalize`, `_fail_permanently`
сначала обновляют задачу, потом блокируют видео). Каскадный DELETE видео блокирует строки
задач; если бы удаление сначала взяло видео, а потом задачи, оно взаимно заблокировалось бы
с финализацией. После блокировки видео набор задач перечитывается: параллельный
`complete_upload` (держал видео) мог добавить новую. Тогда — rollback и повтор, не более
DELETE_ATTEMPTS раз, иначе 503.

Объекты удаляются после commit: если commit не прошёл, файлы живого видео целы. Сбой
хранилища после commit не роняет ответ — остатки уберёт maintenance (сироты yt-media и
yt-raw старше ORPHAN_MEDIA_MINUTES).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.errors import Busy, NotFound
from app.repositories import users as users_repo
from app.repositories import videos as videos_repo
from app.services.uploads import raw_key
from app.services.videos import VIDEO_NOT_FOUND

log = logging.getLogger("services.deletion")
DELETE_ATTEMPTS = 3
VIDEO_BUSY = "Видео сейчас обрабатывается или изменяется. Повторите попытку через несколько секунд"


@dataclass(frozen=True)
class _Leftovers:
    video_id: uuid.UUID
    upload_id: str | None
    files: list[tuple[str, str]]


# Удаление огромного видео (много комментариев, реакций, допусков) может идти дольше общего
# statement_timeout API (5 с → 503): свой предел на эту транзакцию (перенос дня 6 → день 9).
DELETE_STATEMENT_TIMEOUT_MS = 30_000


async def delete_video(db: AsyncSession, video_id: uuid.UUID) -> None:
    for _ in range(DELETE_ATTEMPTS):
        await users_repo.set_local_statement_timeout(db, DELETE_STATEMENT_TIMEOUT_MS)
        jobs = await videos_repo.lock_jobs_of(db, video_id)
        video = await videos_repo.lock_video(db, video_id)
        if video is None:  # удалено параллельно
            await db.rollback()
            raise NotFound(VIDEO_NOT_FOUND)
        if await videos_repo.job_ids_of(db, video_id) == jobs:
            break
        await db.rollback()  # появилась новая задача — взять блокировки заново по порядку
    else:
        raise Busy(VIDEO_BUSY)

    leftovers = _Leftovers(video_id, video.upload_id, await videos_repo.file_keys(db, video_id))
    await videos_repo.delete_video(db, video_id)
    await db.commit()
    await purge_storage(leftovers)


async def purge_storage(leftovers: _Leftovers) -> None:
    """Объекты видео: незавершённый multipart и исходник в yt-raw, файлы из video_files
    и весь префикс videos/{id}/ в yt-media (в том числе загруженное worker-ом, но ещё не
    финализированное)."""
    settings = get_settings()
    raw_bucket, media_bucket = settings.s3_bucket_raw, settings.s3_bucket_media
    key = raw_key(leftovers.video_id)
    if leftovers.upload_id:
        await _quiet(storage.abort_multipart_upload, raw_bucket, key, leftovers.upload_id)
    await _quiet(storage.delete_object, raw_bucket, key)
    for bucket, file_key in leftovers.files:
        await _quiet(storage.delete_object, bucket, file_key)
    try:
        listed = await asyncio.to_thread(
            storage.list_objects, media_bucket, f"videos/{leftovers.video_id}/"
        )
    except Exception:
        log.exception("удаление видео %s: листинг yt-media", leftovers.video_id)
        return
    for media_key, _ in listed:
        await _quiet(storage.delete_object, media_bucket, media_key)


async def _quiet(fn, *args) -> None:
    try:
        await asyncio.to_thread(fn, *args)
    except Exception:
        log.exception("удаление видео: %s%s", fn.__name__, args[:2])
