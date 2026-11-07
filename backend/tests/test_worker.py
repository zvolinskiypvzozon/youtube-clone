"""Worker: очередь (SKIP LOCKED, lease, fencing), ffprobe/ffmpeg, исходы обработки.

Настоящий ffmpeg (образ dev основан на образе worker) и тестовые бакеты хранилища.
Ролики генерируются ffmpeg (lavfi testsrc/sine) — 2 секунды, чтобы тесты были быстрыми.
"""

import asyncio
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import (
    Channel,
    Job,
    JobStatus,
    UserSession,
    Video,
    VideoFile,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from app.repositories import jobs as jobs_repo
from app.repositories import videos as videos_repo
from app.services.uploads import raw_key
from app.worker import processor
from app.worker.main import cleanup_stale_tmp, run_once
from app.worker.maintenance import run_maintenance
from tests.factories import create_user, create_video


def make_video(path: Path, seconds: int = 2, size: str = "1280x720", audio: bool = True) -> None:
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    args += ["-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    subprocess.run([*args, "-f", "mp4", str(path)], check=True)


def make_audio_only(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "2", "-c:a", "aac", "-f", "mp4", str(path)],
        check=True,
    )  # fmt: skip


def make_gif(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
         "-t", "2", "-f", "gif", str(path)],
        check=True,
    )  # fmt: skip


async def seed(
    db: AsyncSession,
    source: Path | bytes,
    *,
    visibility: VideoVisibility = VideoVisibility.public,
    max_attempts: int = 3,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Видео в processing с исходником в yt-test-raw и задачей в очереди: (video_id, job_id)."""
    settings = get_settings()
    user = await create_user(db)
    channel = await db.scalar(select(Channel).where(Channel.owner_id == user.id))
    video = await create_video(db, channel, status=VideoStatus.processing, visibility=visibility)
    key = raw_key(video.id)
    if isinstance(source, bytes):
        await asyncio.to_thread(
            storage.get_s3().put_object, Bucket=settings.s3_bucket_raw, Key=key, Body=source
        )
    else:
        await asyncio.to_thread(
            storage.upload_file,
            str(source),
            settings.s3_bucket_raw,
            key,
            "application/octet-stream",
        )
    await videos_repo.add_file(
        db,
        video_id=video.id,
        kind=VideoFileKind.original,
        bucket=settings.s3_bucket_raw,
        key=key,
        mime="application/octet-stream",
    )
    job = Job(type="process_video", video_id=video.id, max_attempts=max_attempts)
    db.add(job)
    await db.commit()
    return video.id, job.id


def list_keys(bucket: str, prefix: str) -> list[str]:
    resp = storage.get_s3().list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [o["Key"] for o in resp.get("Contents", [])]


async def files_of(db: AsyncSession, video_id: uuid.UUID) -> dict[VideoFileKind, VideoFile]:
    rows = (await db.execute(select(VideoFile).where(VideoFile.video_id == video_id))).scalars()
    return {f.kind: f for f in rows}


async def reload(db: AsyncSession, model, pk):
    db.expire_all()
    return await db.get(model, pk)


# --- успешная обработка ----------------------------------------------------------------


async def test_720p_video_becomes_ready_with_variants_and_thumbnail(
    db: AsyncSession, tmp_path: Path
) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, seconds=2, size="1280x720")
    video_id, job_id = await seed(db, src)

    assert await run_once() == "ready"

    video = await reload(db, Video, video_id)
    assert video.status == VideoStatus.ready
    assert video.duration_seconds == 2
    assert video.published_at is not None  # публичное — опубликовано
    files = await files_of(db, video_id)
    assert set(files) == {VideoFileKind.mp4_360, VideoFileKind.mp4_720, VideoFileKind.thumbnail}
    assert files[VideoFileKind.mp4_360].height == 360
    assert files[VideoFileKind.mp4_720].mime == "video/mp4"
    assert files[VideoFileKind.thumbnail].mime == "image/jpeg"

    settings = get_settings()
    for f in files.values():  # объекты реально лежат в yt-media
        size = await asyncio.to_thread(storage.object_size, settings.s3_bucket_media, f.s3_key)
        assert size == f.size_bytes and size > 0
    # Исходник удалён (диск ВМ ограничен — решение архитектора).
    raw = await asyncio.to_thread(
        storage.object_size_or_none, settings.s3_bucket_raw, raw_key(video_id)
    )
    assert raw is None
    job = await reload(db, Job, job_id)
    assert job.status == JobStatus.done and job.locked_by is None


async def test_small_video_is_not_upscaled(db: AsyncSession, tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240", audio=False)
    video_id, _ = await seed(db, src)

    assert await run_once() == "ready"
    files = await files_of(db, video_id)
    assert VideoFileKind.mp4_720 not in files
    assert files[VideoFileKind.mp4_360].height == 240


async def test_private_video_ready_but_not_published(db: AsyncSession, tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, _ = await seed(db, src, visibility=VideoVisibility.private)

    assert await run_once() == "ready"
    video = await reload(db, Video, video_id)
    assert video.status == VideoStatus.ready and video.published_at is None


async def test_empty_queue_returns_none() -> None:
    assert await run_once() is None


# --- непригодные файлы: failed без повторов ---------------------------------------------


async def _assert_failed(db: AsyncSession, video_id, job_id, reason_part: str) -> None:
    video = await reload(db, Video, video_id)
    assert video.status == VideoStatus.failed
    assert reason_part in video.failure_reason
    job = await reload(db, Job, job_id)
    assert job.status == JobStatus.failed and job.attempts == 1  # без повторов
    raw = await asyncio.to_thread(
        storage.object_size_or_none, get_settings().s3_bucket_raw, raw_key(video_id)
    )
    assert raw is None  # непригодный исходник не храним
    assert VideoFileKind.original not in await files_of(db, video_id)


async def test_corrupted_file_fails_permanently(db: AsyncSession) -> None:
    video_id, job_id = await seed(db, os.urandom(200_000))
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "не является поддерживаемым видео")


async def test_audio_only_file_rejected(db: AsyncSession, tmp_path: Path) -> None:
    src = tmp_path / "audio.mp4"
    make_audio_only(src)
    video_id, job_id = await seed(db, src)
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "нет видеопотока")


async def test_too_long_video_rejected(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_video_seconds", 1)
    src = tmp_path / "in.mp4"
    make_video(src, seconds=2, size="320x240")
    video_id, job_id = await seed(db, src)
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "длиннее 60 минут")


@pytest.mark.parametrize(
    "payload",
    [
        b"ffconcat version 1.0\nfile /proc/1/environ\n",
        b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nfile:///proc/1/environ\n#EXT-X-ENDLIST\n",
    ],
    ids=["concat", "hls"],
)
async def test_playlist_disguised_as_video_rejected(db: AsyncSession, payload: bytes) -> None:
    """Плейлист под видом видео не должен заставить ffmpeg читать /proc/1/environ
    (секреты родительского процесса) и «рисовать» их в кадрах результата."""
    video_id, job_id = await seed(db, payload)
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "не является поддерживаемым видео")
    prefix = f"videos/{video_id}/"
    assert await asyncio.to_thread(list_keys, get_settings().s3_bucket_media, prefix) == []


# --- временные сбои: повтор с паузой ---------------------------------------------------


async def test_transient_error_requeues_with_backoff(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, job_id = await seed(db, src)

    def storage_down(*_: object) -> None:
        raise ConnectionError("хранилище недоступно")

    monkeypatch.setattr(storage, "download_file", storage_down)
    assert await run_once() == "retry"

    job = await reload(db, Job, job_id)
    assert job.status == JobStatus.queued and job.attempts == 1 and job.locked_by is None
    assert "ConnectionError" in job.last_error
    in_future = await db.scalar(select(Job.run_after > text("now()")).where(Job.id == job_id))
    assert in_future  # пауза перед повтором
    assert (await reload(db, Video, video_id)).status == VideoStatus.processing
    assert await run_once() is None  # до истечения паузы задача не берётся


async def test_transient_errors_exhaust_attempts(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, job_id = await seed(db, src, max_attempts=1)
    monkeypatch.setattr(storage, "download_file", lambda *_: (_ for _ in ()).throw(OSError("x")))

    assert await run_once() == "failed"
    assert (await reload(db, Job, job_id)).status == JobStatus.failed
    video = await reload(db, Video, video_id)
    assert video.status == VideoStatus.failed and video.failure_reason


# --- fencing и потеря задачи -----------------------------------------------------------


async def test_stale_worker_cannot_finalize_and_cleans_only_its_objects(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Worker A захватил задачу, его lease истёк, задачу перехватил B (новый токен).
    A доделал рендер — финализировать не может, удаляет только свои объекты."""
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, job_id = await seed(db, src)

    async with get_sessionmaker()() as s:
        claimed_a = await jobs_repo.claim(s, 900)
        await s.commit()
    token_b = uuid.uuid4()
    await db.execute(update(Job).where(Job.id == job_id).values(locked_by=token_b))
    await db.commit()
    # B уже загрузил свой результат под своим ключом — A не должен его удалить.
    media = get_settings().s3_bucket_media
    claimed_b = jobs_repo.Claimed(job_id, video_id, token_b, 2, 3)
    key_b = processor.media_key(claimed_b, "240p.mp4")
    await asyncio.to_thread(storage.get_s3().put_object, Bucket=media, Key=key_b, Body=b"B")

    assert await processor.process(claimed_a) == "lost"

    assert (await reload(db, Video, video_id)).status == VideoStatus.processing
    job = await reload(db, Job, job_id)
    assert job.status == JobStatus.running and job.locked_by == token_b  # B не тронут
    mine = f"videos/{video_id}/{claimed_a.token.hex}/"
    assert await asyncio.to_thread(list_keys, media, mine) == []
    assert await asyncio.to_thread(storage.object_size_or_none, media, key_b) == 1  # B цел


async def test_video_deleted_during_processing(db: AsyncSession, tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, _ = await seed(db, src)
    async with get_sessionmaker()() as s:
        claimed = await jobs_repo.claim(s, 900)
        await s.commit()

    await db.execute(text("DELETE FROM videos WHERE id = :id"), {"id": video_id})
    await db.commit()

    assert await processor.process(claimed) == "lost"
    media = get_settings().s3_bucket_media
    assert await asyncio.to_thread(list_keys, media, f"videos/{video_id}/") == []


async def test_skip_locked_second_worker_does_not_wait(db: AsyncSession) -> None:
    """Пока T1 держит строку задачи (захват не закоммичен), T2 не ждёт и не берёт её."""
    await seed(db, b"x")
    maker = get_sessionmaker()
    async with maker() as t1, maker() as t2:
        first = await jobs_repo.claim(t1, 900)  # не коммитим — строка заблокирована
        assert first is not None
        started = time.monotonic()
        # Таймаут: без SKIP LOCKED захват в T2 ждал бы T1 вечно (тест завис бы, а не упал).
        second = await asyncio.wait_for(jobs_repo.claim(t2, 900), timeout=3)
        assert second is None
        assert time.monotonic() - started < 2
        await t1.rollback()
        await t2.rollback()


# --- периодические задачи --------------------------------------------------------------


async def _expire_running(db: AsyncSession, job_id, attempts: int) -> None:
    await db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.running,
            attempts=attempts,
            locked_by=uuid.uuid4(),
            locked_until=text("now() - interval '1 minute'"),
        )
    )
    await db.commit()


async def test_expired_lease_with_attempts_left_is_requeued(db: AsyncSession) -> None:
    video_id, job_id = await seed(db, b"x")
    await _expire_running(db, job_id, attempts=1)

    report = await run_maintenance()
    assert report.requeued == 1
    job = await reload(db, Job, job_id)
    assert job.status == JobStatus.queued and job.locked_by is None
    assert (await reload(db, Video, video_id)).status == VideoStatus.processing


async def test_expired_lease_without_attempts_fails_video(db: AsyncSession) -> None:
    video_id, job_id = await seed(db, b"x")
    await _expire_running(db, job_id, attempts=3)

    report = await run_maintenance()
    assert report.exhausted == 1
    assert (await reload(db, Job, job_id)).status == JobStatus.failed
    video = await reload(db, Video, video_id)
    assert video.status == VideoStatus.failed and video.failure_reason
    raw = await asyncio.to_thread(
        storage.object_size_or_none, get_settings().s3_bucket_raw, raw_key(video_id)
    )
    assert raw is None


async def test_maintenance_removes_expired_sessions_and_stale_uploads(
    db: AsyncSession,
) -> None:
    user = await create_user(db)
    channel = await db.scalar(select(Channel).where(Channel.owner_id == user.id))
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=os.urandom(32),
            created_at=text("now() - interval '40 days'"),
            expires_at=text("now() - interval '10 days'"),
        )
    )
    stale = await create_video(db, channel, status=VideoStatus.uploading)
    fresh = await create_video(db, channel, status=VideoStatus.uploading)
    await db.execute(
        update(Video)
        .where(Video.id == stale.id)
        .values(created_at=text("now() - interval '2 days'"))
    )
    await db.commit()
    stale_id, fresh_id = stale.id, fresh.id  # reload() делает expire_all
    report = await run_maintenance()
    assert report.sessions_deleted >= 1 and report.stale_uploads == 1
    assert await reload(db, Video, stale_id) is None
    assert await reload(db, Video, fresh_id) is not None


async def test_format_outside_whitelist_rejected(db: AsyncSession, tmp_path: Path) -> None:
    """Анимированный GIF — настоящий видеопоток с длительностью, ffmpeg его прочитал бы,
    но демультиплексора gif нет в белом списке. Тест доказывает, что белый список реально
    применяется (HLS/concat-трюки ffmpeg 7.1 отклоняет и сам — по ним этого не видно)."""
    src = tmp_path / "anim.gif"
    make_gif(src)
    video_id, job_id = await seed(db, src)
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "не является поддерживаемым видео")


async def test_worker_stop_requeues_without_burning_attempt(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM посреди обработки: задача сразу снова в очереди (не ждём истечения lease),
    попытка не засчитывается."""
    src = tmp_path / "in.mp4"
    make_video(src, size="1280x720")
    video_id, job_id = await seed(db, src)

    started = asyncio.Event()
    real_transcode = processor.media.transcode

    async def slow_720(src_, dst, height, *args: object) -> None:
        if height < 720:  # 360p — по-настоящему (успеет загрузиться в yt-media)
            return await real_transcode(src_, dst, height, *args)
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(processor.media, "transcode", slow_720)
    async with get_sessionmaker()() as s:
        claimed = await jobs_repo.claim(s, 900)
        await s.commit()
    task = asyncio.create_task(processor.process(claimed))
    await asyncio.wait_for(started.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    job = await reload(db, Job, job_id)
    assert job.status == JobStatus.queued and job.locked_by is None
    assert job.attempts == 0
    assert (await reload(db, Video, video_id)).status == VideoStatus.processing
    # Уже загруженный 360p этой попытки удалён — следующая попытка будет с новым токеном.
    prefix = f"videos/{video_id}/{claimed.token.hex}/"
    assert await asyncio.to_thread(list_keys, get_settings().s3_bucket_media, prefix) == []


def ffprobe_tags(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        check=True,
        capture_output=True,
    ).stdout
    import json

    return json.loads(out).get("format", {}).get("tags", {})


def make_video_with_metadata(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
         "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-metadata", "title=SECRET_TITLE", "-metadata", "location=+55.7558+037.6173/",
         "-f", "mp4", str(path)],
        check=True,
    )  # fmt: skip


def make_huge_frame(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=4200x2200:rate=1",
         "-t", "1", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-f", "mp4", str(path)],
        check=True,
    )  # fmt: skip


async def test_source_metadata_not_published(db: AsyncSession, tmp_path: Path) -> None:
    """Метаданные исходника (название, GPS-координаты с телефона) не попадают в результат."""
    src = tmp_path / "in.mp4"
    make_video_with_metadata(src)
    assert ffprobe_tags(src).get("location")  # в исходнике координаты есть
    video_id, _ = await seed(db, src)
    assert await run_once() == "ready"

    f = (await files_of(db, video_id))[VideoFileKind.mp4_360]
    out = tmp_path / "out.mp4"
    await asyncio.to_thread(
        storage.download_file, get_settings().s3_bucket_media, f.s3_key, str(out)
    )
    tags = ffprobe_tags(out)
    assert "location" not in tags and "title" not in tags


async def test_oversized_resolution_rejected(db: AsyncSession, tmp_path: Path) -> None:
    src = tmp_path / "huge.mp4"
    make_huge_frame(src)
    video_id, job_id = await seed(db, src)
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "разрешение")


async def test_missing_source_fails_without_retries(db: AsyncSession) -> None:
    """Исходник пропал из хранилища (строго консистентного) — повторы бесполезны."""
    video_id, job_id = await seed(db, b"x")
    await asyncio.to_thread(storage.delete_object, get_settings().s3_bucket_raw, raw_key(video_id))
    assert await run_once() == "failed"
    await _assert_failed(db, video_id, job_id, "Исходный файл не найден")


async def test_heartbeat_detects_lost_lease_and_cleans_up(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="1280x720")
    video_id, _ = await seed(db, src)
    monkeypatch.setattr(get_settings(), "worker_heartbeat_seconds", 0.1)

    async def lease_gone(*_: object) -> bool:
        return False

    monkeypatch.setattr(jobs_repo, "extend_lease", lease_gone)
    async with get_sessionmaker()() as s:
        claimed = await jobs_repo.claim(s, 900)
        await s.commit()

    assert await processor.process(claimed) == "lost"
    prefix = f"videos/{video_id}/{claimed.token.hex}/"
    assert await asyncio.to_thread(list_keys, get_settings().s3_bucket_media, prefix) == []


async def test_ambiguous_commit_does_not_delete_published_files(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit финализации выполнился, но ответ «потерялся» (обрыв соединения).
    Файлы готового видео удалять нельзя — иначе ready-видео с 404 навсегда."""
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, _ = await seed(db, src)
    real_finalize = processor._finalize

    async def finalize_then_disconnect(*args: object) -> str | None:
        await real_finalize(*args)
        raise ConnectionResetError("соединение оборвалось после commit")

    monkeypatch.setattr(processor, "_finalize", finalize_then_disconnect)
    assert await run_once() == "lost"

    assert (await reload(db, Video, video_id)).status == VideoStatus.ready
    media_bucket = get_settings().s3_bucket_media
    for f in (await files_of(db, video_id)).values():
        size = await asyncio.to_thread(storage.object_size_or_none, media_bucket, f.s3_key)
        assert size, f"{f.kind}: файл готового видео удалён"


async def test_stale_cleanup_does_not_touch_video_that_moved_to_processing(
    db: AsyncSession,
) -> None:
    """Загрузка висела дольше порога, но пользователь успел её завершить: видео в processing.
    Уборка не должна удалить ни строку, ни исходник."""
    video_id, _ = await seed(db, b"x")  # processing, исходник в yt-test-raw
    await db.execute(
        update(Video)
        .where(Video.id == video_id)
        .values(created_at=text("now() - interval '3 days'"))
    )
    await db.commit()

    await run_maintenance()
    assert await reload(db, Video, video_id) is not None
    raw = await asyncio.to_thread(
        storage.object_size_or_none, get_settings().s3_bucket_raw, raw_key(video_id)
    )
    assert raw == 1


async def test_orphan_media_swept_referenced_kept(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "in.mp4"
    make_video(src, size="320x240")
    video_id, _ = await seed(db, src)
    assert await run_once() == "ready"
    media_bucket = get_settings().s3_bucket_media
    orphan = f"videos/{video_id}/{uuid.uuid4().hex}/360p.mp4"  # от прерванной попытки
    await asyncio.to_thread(storage.get_s3().put_object, Bucket=media_bucket, Key=orphan, Body=b"o")
    monkeypatch.setattr(get_settings(), "orphan_media_minutes", -1)  # «всё уже старое»

    report = await run_maintenance()
    assert report.orphan_objects >= 1
    assert await asyncio.to_thread(storage.object_size_or_none, media_bucket, orphan) is None
    for f in (await files_of(db, video_id)).values():
        assert await asyncio.to_thread(storage.object_size_or_none, media_bucket, f.s3_key)


def test_stale_tmp_dirs_removed_on_start(tmp_path: Path) -> None:
    (tmp_path / "job-123-abc").mkdir()
    (tmp_path / "job-123-abc" / "input").write_bytes(b"x" * 1000)
    (tmp_path / "other").mkdir()
    assert cleanup_stale_tmp(tmp_path) == 1
    assert not (tmp_path / "job-123-abc").exists() and (tmp_path / "other").exists()


def test_worker_process_is_not_dumpable() -> None:
    """PR_SET_DUMPABLE=0 закрывает /proc/self/environ от дочернего процесса того же uid
    (эксплойт в ffmpeg не прочитает секреты worker-а). Проверка в отдельном процессе."""
    code = (
        "import subprocess;"
        "from app.worker.main import set_non_dumpable;"
        "assert set_non_dumpable();"
        "import os;"
        "r = subprocess.run(['cat', f'/proc/{os.getpid()}/environ'], capture_output=True,"
        " env={'PATH': '/usr/bin:/bin'});"
        "print(r.returncode, r.stderr.decode())"
    )
    out = subprocess.run(
        ["python", "-c", code], capture_output=True, text=True, check=True, env={**os.environ}
    ).stdout
    assert out.startswith("1 ") and "Permission denied" in out
