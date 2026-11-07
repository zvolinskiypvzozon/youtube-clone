"""Периодические задачи worker (раз в WORKER_MAINTENANCE_SECONDS).

- задачи упавших worker-ов (lease истёк): обратно в очередь или failed по лимиту попыток;
- истёкшие сессии: удаляются (ADR 0005);
- брошенные загрузки (uploading дольше STALE_UPLOAD_HOURS): строка удаляется условным
  DELETE … RETURNING, затем — abort multipart и удаление объекта в хранилище;
- осиротевшие объекты в yt-media: префиксы videos/{id}/{token}/, на которые не ссылается
  ни одна строка video_files и которые старше ORPHAN_MEDIA_MINUTES (остатки после SIGKILL
  worker-а или неоднозначного commit финализации).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core import storage
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.repositories import jobs as jobs_repo
from app.repositories import sessions as sessions_repo
from app.repositories import videos as videos_repo
from app.services.uploads import raw_key

log = logging.getLogger("worker.maintenance")
INTERRUPTED = "Обработка прерывалась слишком много раз. Попробуйте загрузить видео снова"


@dataclass(frozen=True)
class MaintenanceReport:
    requeued: int
    exhausted: int
    sessions_deleted: int
    stale_uploads: int
    orphan_objects: int


async def run_maintenance() -> MaintenanceReport:
    settings = get_settings()
    async with get_sessionmaker()() as db:
        requeued, exhausted = await jobs_repo.reclaim_expired(db)
        await jobs_repo.fail_videos(db, exhausted, INTERRUPTED)
        originals = await videos_repo.original_keys(db, exhausted)
        await videos_repo.delete_originals(db, exhausted)
        sessions = await sessions_repo.delete_expired(db)
        stale = await videos_repo.delete_stale_uploads(db, settings.stale_upload_hours)
        await db.commit()

    # Хранилище — только после commit и только для того, что реально удалено в БД.
    for bucket, key in originals:
        await _quiet(storage.delete_object, bucket, key)
    for video_id, upload_id in stale:
        key = raw_key(video_id)
        if upload_id:
            await _quiet(storage.abort_multipart_upload, settings.s3_bucket_raw, key, upload_id)
        await _quiet(storage.delete_object, settings.s3_bucket_raw, key)

    orphans = await sweep_orphan_media()
    report = MaintenanceReport(requeued, len(exhausted), sessions, len(stale), orphans)
    if any(vars(report).values()):
        log.info("maintenance: %s", report)
    return report


async def sweep_orphan_media() -> int:
    """Удаляет объекты yt-media без ссылок из БД. Порог возраста — больше lease: объекты
    задачи, которая ещё обрабатывается (её строки появятся только при финализации),
    не моложе порога не бывают."""
    settings = get_settings()
    bucket = settings.s3_bucket_media
    async with get_sessionmaker()() as db:
        referenced = await videos_repo.referenced_media_prefixes(db, bucket)
        await db.commit()
    objects = await asyncio.to_thread(storage.list_objects, bucket, "videos/")
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.orphan_media_minutes)
    orphans = [
        key
        for key, modified in objects
        if key.rsplit("/", 1)[0] + "/" not in referenced and modified < cutoff
    ]
    if orphans:
        await _quiet(storage.delete_objects, bucket, orphans)
    return len(orphans)


async def _quiet(fn, *args) -> None:
    try:
        await asyncio.to_thread(fn, *args)
    except Exception:
        log.exception("maintenance: %s", fn.__name__)
