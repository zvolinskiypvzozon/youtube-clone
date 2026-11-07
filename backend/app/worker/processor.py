"""Обработка одной задачи process_video (ADR 0003).

Шаги: скачать исходник → ffprobe (реальное видео, ≤ 60 мин) → MP4 360p/720p → превью →
загрузить в yt-media → финализация одной транзакцией с fencing → удалить исходник.

Исходы:
  ready  — видео готово;
  failed — файл непригоден (MediaError): повтор бесполезен, видео failed с причиной;
  retry  — временный сбой (хранилище, БД): задача снова в очереди с паузой, пока есть попытки;
  lost   — задачу перехватили (наш lease истёк) или видео удалено: результат отбрасывается,
           загруженные нами объекты удаляются.
"""

import asyncio
import contextlib
import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from botocore.exceptions import ClientError

from app.core import storage
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import VideoFileKind, VideoStatus, VideoVisibility
from app.repositories import jobs as jobs_repo
from app.repositories import videos as videos_repo
from app.repositories.jobs import Claimed
from app.worker import media

log = logging.getLogger("worker.processor")

Outcome = Literal["ready", "failed", "retry", "lost"]
GENERIC_FAILURE = "Не удалось обработать видео. Попробуйте загрузить его снова"
TOO_LONG = "Видео длиннее 60 минут"
SOURCE_MISSING = "Исходный файл не найден. Загрузите видео снова"
TMP_PREFIX = "job-"  # временные каталоги задач; остатки после падения чистит main при старте


class LeaseLost(Exception):
    """Задачу перехватил другой worker или видео удалено."""


@dataclass(frozen=True)
class Produced:
    kind: VideoFileKind
    key: str
    mime: str
    size: int
    width: int | None
    height: int | None


def media_key(claimed: Claimed, name: str) -> str:
    """Ключ включает fencing-токен попытки. Иначе worker, потерявший lease, удаляя «свои»
    объекты, удалил бы файлы того, кто перехватил задачу и уже финализировал видео
    (ключи совпали бы)."""
    return f"videos/{claimed.video_id}/{claimed.token.hex}/{name}"


async def _heartbeat(claimed: Claimed, lost: asyncio.Event) -> None:
    settings = get_settings()
    while True:
        await asyncio.sleep(settings.worker_heartbeat_seconds)
        try:
            async with get_sessionmaker()() as db:
                alive = await jobs_repo.extend_lease(
                    db, claimed.job_id, claimed.token, settings.worker_lease_seconds
                )
                await db.commit()
        except Exception:
            log.exception("heartbeat job=%s: сбой продления lease", claimed.job_id)
            continue
        if not alive:
            lost.set()
            return


def _check(lost: asyncio.Event) -> None:
    if lost.is_set():
        raise LeaseLost


async def _render(
    claimed: Claimed, tmp: Path, lost: asyncio.Event, produced: list[Produced]
) -> media.Probe:
    """Рендер; produced пополняется сразу после загрузки каждого объекта — чтобы при
    перехвате задачи или сбое посреди работы вызывающий удалил всё уже загруженное."""
    settings = get_settings()
    async with get_sessionmaker()() as db:
        original = await videos_repo.get_original(db, claimed.video_id)
        await db.commit()
    if original is None:
        raise LeaseLost  # видео удалено вместе с файлами (каскад)

    src = tmp / "input"  # без расширения: ffmpeg не угадывает формат по имени
    try:
        await asyncio.to_thread(storage.download_file, original.bucket, original.s3_key, str(src))
    except ClientError as exc:
        # Хранилище строго консистентно: 404 при живой строке original не исправится
        # повторами — сразу failed, а не три попытки с паузами.
        if storage.error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            raise media.MediaError(SOURCE_MISSING) from exc
        raise
    _check(lost)

    probe = await media.probe(src)
    if probe.duration > settings.max_video_seconds:
        raise media.MediaError(TOO_LONG)

    bucket = settings.s3_bucket_media
    for kind_name, height in media.variant_heights(probe.height).items():
        _check(lost)
        dst = tmp / f"{kind_name}.mp4"
        await media.transcode(
            src, dst, height, probe.duration, settings.ffmpeg_threads, settings.max_video_seconds
        )
        key = media_key(claimed, f"{height}p.mp4")
        await asyncio.to_thread(storage.upload_file, str(dst), bucket, key, "video/mp4")
        produced.append(
            Produced(VideoFileKind(kind_name), key, "video/mp4", media.file_size(dst), None, height)
        )

    _check(lost)
    thumb = tmp / "thumb.jpg"
    await media.thumbnail(src, thumb, probe.duration, settings.ffmpeg_threads)
    key = media_key(claimed, "thumb.jpg")
    await asyncio.to_thread(storage.upload_file, str(thumb), bucket, key, "image/jpeg")
    produced.append(
        Produced(VideoFileKind.thumbnail, key, "image/jpeg", media.file_size(thumb), 640, None)
    )
    return probe


async def _finalize(
    claimed: Claimed, probe: media.Probe, produced: list[Produced], commit_started: list[bool]
) -> str | None:
    """Одна транзакция: задача done (только с нашим токеном), файлы, статус ready.
    Возвращает ключ исходника для удаления (None — исходника нет).
    LeaseLost — задачу перехватили или видео удалено.

    commit_started выставляется перед commit: если commit упадёт «неоднозначно» (выполнился
    на сервере, но ответ не дошёл), вызывающий не должен удалять объекты, на которые,
    возможно, уже ссылается готовое видео."""
    settings = get_settings()
    async with get_sessionmaker()() as db:
        if not await jobs_repo.mark_done(db, claimed.job_id, claimed.token):
            await db.rollback()
            raise LeaseLost
        video = await videos_repo.lock_video(db, claimed.video_id)
        if video is None:
            await db.rollback()
            raise LeaseLost
        original = await videos_repo.get_original(db, claimed.video_id)
        raw_key = original.s3_key if original else None
        await videos_repo.delete_original(db, claimed.video_id)
        for f in produced:
            await videos_repo.add_file(
                db,
                video_id=claimed.video_id,
                kind=f.kind,
                bucket=settings.s3_bucket_media,
                key=f.key,
                mime=f.mime,
                size_bytes=f.size,
                width=f.width,
                height=f.height,
            )
        video.status = VideoStatus.ready
        video.failure_reason = None
        video.duration_seconds = round(probe.duration)
        if video.visibility != VideoVisibility.private and video.published_at is None:
            video.published_at = datetime.now(UTC)
        commit_started.append(True)
        await db.commit()
        return raw_key


async def _fail_permanently(claimed: Claimed, reason: str) -> bool:
    """failed с причиной (с fencing). False — задача уже не наша (перехвачена/удалена)."""
    settings = get_settings()
    raw_key = None
    async with get_sessionmaker()() as db:
        if not await jobs_repo.mark_failed(db, claimed.job_id, claimed.token, reason):
            await db.rollback()
            return False
        video = await videos_repo.lock_video(db, claimed.video_id)
        if video is not None:
            video.status = VideoStatus.failed
            video.failure_reason = reason
            original = await videos_repo.get_original(db, claimed.video_id)
            raw_key = original.s3_key if original else None
            await videos_repo.delete_original(db, claimed.video_id)
        await db.commit()
    if raw_key:  # исходник непригоден — не храним (диск ВМ ограничен)
        await _quiet_delete(settings.s3_bucket_raw, [raw_key])
    return True


async def _retry_or_fail(claimed: Claimed, error: str) -> Outcome:
    settings = get_settings()
    if claimed.attempts >= claimed.max_attempts:
        return "failed" if await _fail_permanently(claimed, GENERIC_FAILURE) else "lost"
    delay = settings.worker_retry_base_seconds * 2 ** (claimed.attempts - 1)
    async with get_sessionmaker()() as db:
        owned = await jobs_repo.requeue(db, claimed.job_id, claimed.token, error[:500], delay)
        await db.commit()
    return "retry" if owned else "lost"


async def _quiet_delete(bucket: str, keys: list[str]) -> None:
    if not keys:
        return
    try:
        await asyncio.to_thread(storage.delete_objects, bucket, keys)
    except Exception:
        log.exception("не удалось удалить объекты %s/%s", bucket, keys)


async def process(claimed: Claimed) -> Outcome:
    """Обработка задачи. Удаление своих объектов из yt-media — во всех исходах, кроме
    успешного и «commit начат»: там объекты либо уже принадлежат готовому видео, либо
    станут сиротами, которых уберёт периодическая чистка (maintenance)."""
    settings = get_settings()
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(claimed, lost))
    produced: list[Produced] = []
    commit_started: list[bool] = []
    media_bucket = settings.s3_bucket_media

    async def discard() -> None:
        if not commit_started:
            await _quiet_delete(media_bucket, [p.key for p in produced])

    try:
        with tempfile.TemporaryDirectory(prefix=f"{TMP_PREFIX}{claimed.job_id}-") as tmp:
            probe = await _render(claimed, Path(tmp), lost, produced)
        raw_key = await _finalize(claimed, probe, produced, commit_started)
        if raw_key:
            await _quiet_delete(settings.s3_bucket_raw, [raw_key])
        log.info("job=%s video=%s: готово", claimed.job_id, claimed.video_id)
        return "ready"
    except LeaseLost:
        log.warning(
            "job=%s: задача перехвачена или видео удалено — результат отброшен", claimed.job_id
        )
        await discard()
        return "lost"
    except media.MediaError as exc:
        log.info("job=%s video=%s: непригодный файл: %s", claimed.job_id, claimed.video_id, exc)
        await discard()
        return "failed" if await _fail_permanently(claimed, str(exc)) else "lost"
    except asyncio.CancelledError:
        # Остановка worker (SIGTERM): убрать своё и сразу вернуть задачу в очередь,
        # не дожидаясь истечения lease; попытка не засчитывается.
        await discard()
        async with get_sessionmaker()() as db:
            await jobs_repo.requeue(
                db, claimed.job_id, claimed.token, "worker остановлен", 0, refund_attempt=True
            )
            await db.commit()
        raise
    except Exception as exc:
        if commit_started:
            # Commit мог выполниться на сервере: объекты, возможно, уже у готового видео.
            # Не трогаем; если commit не прошёл — задача вернётся по истечении lease,
            # а осиротевшие объекты уберёт maintenance.
            log.exception("job=%s: сбой во время commit финализации", claimed.job_id)
            return "lost"
        log.exception("job=%s: временный сбой", claimed.job_id)
        await discard()
        return await _retry_or_fail(claimed, f"{type(exc).__name__}: {exc}")
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
