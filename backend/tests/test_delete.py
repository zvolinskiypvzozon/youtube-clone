"""Удаление своего видео: DELETE /api/videos/{id}, уборка сирот yt-raw, lock_timeout.

Права — как у правки (ADR 0006, доп. 2). Порядок блокировок «задачи → видео» (как у
worker-а), объекты хранилища — после commit (services/deletion.py).
"""

import asyncio
import time
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models import Job, User, UserRole, Video, VideoAccess, VideoFile, VideoFileKind
from app.repositories import jobs as jobs_repo
from app.repositories import videos as videos_repo
from app.services import deletion
from app.services import manage as manage_service
from app.services import uploads as uploads_service
from app.services.uploads import raw_key
from app.worker import maintenance, media, processor
from tests.helpers import (
    MANAGE_VIEWERS,
    PRIV,
    PROC,
    PUB,
    api_user,
    auth_headers,
    backend_pid,
    channel_of,
    manage_expected,
    manage_matrix,
    seed_video,
    wait_for_lock_waiters,
)


def _settings():
    return get_settings()


def _keys(bucket: str, prefix: str) -> list[str]:
    return [key for key, _ in storage.list_objects(bucket, prefix)]


async def _media_keys(video_id: uuid.UUID) -> list[str]:
    return await asyncio.to_thread(_keys, _settings().s3_bucket_media, f"videos/{video_id}/")


async def _raw_exists(video_id: uuid.UUID) -> bool:
    size = await asyncio.to_thread(
        storage.object_size_or_none, _settings().s3_bucket_raw, raw_key(video_id)
    )
    return size is not None


async def _uploads_of(video_id: uuid.UUID) -> list[str]:
    uploads = await asyncio.to_thread(
        storage.list_multipart_uploads, _settings().s3_bucket_raw, raw_key(video_id)
    )
    return [upload_id for key, upload_id, _ in uploads if key == raw_key(video_id)]


async def _rows(db: AsyncSession, model, video_id: uuid.UUID) -> int:
    column = model.id if model is Video else model.video_id
    n = await db.scalar(select(func.count()).select_from(model).where(column == video_id))
    await db.commit()
    return n or 0


async def _assert_gone(db: AsyncSession, video_id: uuid.UUID) -> None:
    for model in (Video, VideoFile, VideoAccess, Job):
        assert await _rows(db, model, video_id) == 0, model.__name__
    assert await _media_keys(video_id) == []
    assert not await _raw_exists(video_id)
    assert await _uploads_of(video_id) == []


async def _start_upload(client: AsyncClient, user: dict, payload: bytes) -> tuple[str, list]:
    """Начатая загрузка с залитыми частями (как браузер), без complete."""
    r = await client.post(
        "/api/uploads",
        json={
            "title": "Загрузка",
            "description": "",
            "visibility": "public",
            "content_type": "video/mp4",
            "size_bytes": len(payload),
        },
        headers=user["auth"],
    )
    assert r.status_code == 201, r.text
    upload = r.json()
    parts = []
    async with httpx.AsyncClient(timeout=60) as s3:
        for part in upload["parts"]:
            put = await s3.put(part["url"], content=payload)
            assert put.status_code == 200, put.text
            parts.append((part["number"], put.headers["etag"]))
    return upload["video_id"], parts


# --- Удаление владельцем --------------------------------------------------------------------


async def test_owner_deletes_ready_video(client: AsyncClient, db: AsyncSession) -> None:
    owner, viewer = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    db.add(VideoAccess(video_id=video.id, user_id=uuid.UUID(viewer["id"])))
    await db.commit()
    assert len(await _media_keys(video.id)) == 3

    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text

    await _assert_gone(db, video.id)
    for path in ("", "/stream"):
        r = await client.get(f"/api/videos/{video.id}{path}", headers=owner["auth"])
        assert r.status_code == 404, path
    feed = await client.get("/api/videos")
    assert str(video.id) not in {item["id"] for item in feed.json()["items"]}
    # Повтор — 404: видео уже нет.
    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 404


async def test_delete_uploading_aborts_multipart(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video_id, _ = await _start_upload(client, owner, b"x" * 1000)
    vid = uuid.UUID(video_id)
    assert len(await _uploads_of(vid)) == 1

    r = await client.delete(f"/api/videos/{video_id}", headers=owner["auth"])
    assert r.status_code == 204, r.text
    await _assert_gone(db, vid)


async def test_delete_processing_removes_queued_job(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV, PROC)
    await videos_repo.enqueue_processing(db, video.id)
    await db.commit()

    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text
    await _assert_gone(db, video.id)  # в том числе строки задачи


async def test_storage_failure_after_commit_is_swept_later(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Хранилище недоступно после commit — ответ 204 (строки удалены), остатки убирает
    maintenance: yt-media — уборка сирот по ссылкам, yt-raw — по отсутствию строки видео."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    raw_bucket = _settings().s3_bucket_raw
    await asyncio.to_thread(
        storage.get_s3().put_object, Bucket=raw_bucket, Key=raw_key(video.id), Body=b"raw"
    )

    def down(*_args, **_kwargs):
        raise OSError("хранилище недоступно")

    with monkeypatch.context() as m:
        m.setattr(storage, "delete_object", down)
        m.setattr(storage, "list_objects", down)
        r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text
    assert await _rows(db, Video, video.id) == 0
    assert len(await _media_keys(video.id)) == 3 and await _raw_exists(video.id)

    monkeypatch.setattr(_settings(), "orphan_media_minutes", -1)  # «всё уже старое»
    report = await maintenance.run_maintenance()
    assert report.orphan_raw >= 1
    await _assert_gone(db, video.id)


async def test_failed_commit_keeps_video_and_files(client: AsyncClient, db: AsyncSession) -> None:
    """Commit удаления не прошёл — видео и все его файлы на месте: объекты удаляются только
    после успешного commit."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async with get_sessionmaker()() as delete_db:

        async def broken_commit() -> None:
            raise ConnectionError("соединение с БД потеряно")

        delete_db.commit = broken_commit  # type: ignore[method-assign]
        viewer = await delete_db.get(User, uuid.UUID(owner["id"]))
        with pytest.raises(ConnectionError):
            await manage_service.delete_video(delete_db, viewer, video.id)
        await delete_db.rollback()

    assert await _rows(db, Video, video.id) == 1
    assert len(await _media_keys(video.id)) == 3
    r = await client.get(f"/api/videos/{video.id}/stream")
    assert r.status_code == 200, r.text


# --- Отказы: 401 / 403 / 404 ----------------------------------------------------------------


@pytest.mark.parametrize("viewer_kind", MANAGE_VIEWERS)
async def test_only_owner_deletes(client: AsyncClient, db: AsyncSession, viewer_kind: str) -> None:
    owner = await api_user(client, db)
    videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
    for name, video in videos.items():
        r = await client.delete(f"/api/videos/{video.id}", headers=auth_headers(viewer))
        assert r.status_code == manage_expected(viewer_kind, name), (name, r.text)
        assert await _rows(db, Video, video.id) == 1, name
        if name not in ("processing", "failed"):  # у них нет файлов
            assert len(await _media_keys(video.id)) == 3, name


async def test_not_found_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    hidden = await client.delete(f"/api/videos/{private.id}", headers=stranger["auth"])
    missing = await client.delete(f"/api/videos/{uuid.uuid4()}", headers=stranger["auth"])
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


async def test_delete_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    s = uuid.uuid4().hex[:8]
    r = await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    video = await seed_video(db, await channel_of(db, {"id": r.json()["id"]}), PRIV)
    r = await client.delete(f"/api/videos/{video.id}")
    assert r.status_code == 403
    assert await _rows(db, Video, video.id) == 1
    r = await client.delete(
        f"/api/videos/{video.id}", headers={"X-CSRF-Token": client.cookies["csrf"]}
    )
    assert r.status_code == 204, r.text
    assert await _rows(db, Video, video.id) == 0


@pytest.mark.parametrize("role", [UserRole.user, UserRole.moderator])
async def test_denied_delete_does_not_wait_for_row_lock(
    client: AsyncClient, db: AsyncSession, role: UserRole
) -> None:
    """Права — до блокировок: чужой DELETE не встаёт в очередь на строки видео и задач."""
    owner, other = await api_user(client, db), await api_user(client, db, role)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await videos_repo.enqueue_processing(db, video.id)
    await db.commit()

    async with get_sessionmaker()() as holder:
        await videos_repo.lock_jobs_of(holder, video.id)
        assert await videos_repo.lock_video(holder, video.id) is not None
        r = await asyncio.wait_for(
            client.delete(f"/api/videos/{video.id}", headers=other["auth"]), timeout=3
        )
        assert r.status_code == 403, r.text
        await holder.rollback()


# --- lock_timeout ---------------------------------------------------------------------------


async def test_busy_video_returns_503_after_lock_timeout(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Строка видео занята дольше lock_timeout API (5 с) — 503 с Retry-After, видео цело."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    timeout = _settings().db_lock_timeout_ms / 1000

    async with get_sessionmaker()() as holder:
        assert await videos_repo.lock_video(holder, video.id) is not None
        started = time.monotonic()
        r = await asyncio.wait_for(
            client.delete(f"/api/videos/{video.id}", headers=owner["auth"]), timeout=timeout + 5
        )
        elapsed = time.monotonic() - started
        await holder.rollback()

    assert r.status_code == 503, r.text
    assert r.headers["retry-after"] == "2"
    assert timeout - 0.5 <= elapsed < timeout + 3
    assert await _rows(db, Video, video.id) == 1
    # Блокировка снята — удаление проходит.
    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text


# --- Гонки (настоящий код, каждая сторона в своей сессии БД) --------------------------------


async def _claimed_processing(db: AsyncSession, owner: dict):
    video = await seed_video(db, await channel_of(db, owner), PUB, PROC)
    await videos_repo.enqueue_processing(db, video.id)
    await db.commit()
    async with get_sessionmaker()() as s:
        claimed = await jobs_repo.claim(s, 900)
        await s.commit()
    assert claimed is not None and claimed.video_id == video.id
    return video, claimed


async def _produce(claimed) -> list[processor.Produced]:
    """Объект, загруженный worker-ом до финализации (videos/{id}/{token}/...)."""
    key = processor.media_key(claimed, "360p.mp4")
    await asyncio.to_thread(
        storage.get_s3().put_object,
        Bucket=_settings().s3_bucket_media,
        Key=key,
        Body=b"360" * 100,
        ContentType="video/mp4",
    )
    return [processor.Produced(VideoFileKind.mp4_360, key, "video/mp4", 300, None, 360)]


PROBE = media.Probe(duration=5.0, width=320, height=240, has_audio=True)


async def test_delete_waits_for_finalize(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настоящий `_finalize` держит задачу и видео (commit не сделан) → DELETE ждёт, затем
    удаляет готовое видео вместе с файлами финализации."""
    owner = await api_user(client, db)
    video, claimed = await _claimed_processing(db, owner)
    produced = await _produce(claimed)

    holding, release = asyncio.Event(), asyncio.Event()
    finalize_pid: list[int] = []
    real_add_file = videos_repo.add_file

    async def gated_add_file(session, **kwargs):
        # Вызывается в _finalize уже под блокировками задачи и видео.
        finalize_pid.append(await backend_pid(session))
        holding.set()
        await release.wait()
        return await real_add_file(session, **kwargs)

    monkeypatch.setattr(videos_repo, "add_file", gated_add_file)
    finalize = asyncio.create_task(processor._finalize(claimed, PROBE, produced, []))
    await asyncio.wait_for(holding.wait(), timeout=5)

    delete = asyncio.create_task(client.delete(f"/api/videos/{video.id}", headers=owner["auth"]))
    await wait_for_lock_waiters(finalize_pid[0])
    release.set()
    await finalize
    r = await delete
    assert r.status_code == 204, r.text
    await _assert_gone(db, video.id)


async def test_finalize_after_delete_loses_lease(client: AsyncClient, db: AsyncSession) -> None:
    """DELETE держит задачу и видео (commit не сделан) → настоящий `_finalize` ждёт на строке
    задачи и после commit удаления получает LeaseLost; heartbeat тоже видит потерю."""
    owner = await api_user(client, db)
    video, claimed = await _claimed_processing(db, owner)
    produced = await _produce(claimed)

    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as delete_db:
        delete_pid = await backend_pid(delete_db)
        real_commit = delete_db.commit

        async def gated_commit() -> None:
            holding.set()
            await release.wait()
            await real_commit()

        delete_db.commit = gated_commit  # type: ignore[method-assign]
        viewer = await delete_db.get(User, uuid.UUID(owner["id"]))
        delete = asyncio.create_task(manage_service.delete_video(delete_db, viewer, video.id))
        await asyncio.wait_for(holding.wait(), timeout=5)

        finalize = asyncio.create_task(processor._finalize(claimed, PROBE, produced, []))
        await wait_for_lock_waiters(delete_pid)
        release.set()
        await delete
        with pytest.raises(processor.LeaseLost):
            await finalize

    async with get_sessionmaker()() as s:
        assert not await jobs_repo.extend_lease(s, claimed.job_id, claimed.token, 900)
        await s.rollback()
    await _assert_gone(db, video.id)


async def test_delete_retries_after_concurrent_complete_upload(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`complete_upload` держит видео и уже вставил задачу (commit не сделан) → DELETE
    заблокировал задачи (их ещё нет) и ждёт видео. После commit загрузки набор задач другой —
    удаление повторяет захват блокировок по порядку и удаляет видео, задачу и исходник."""
    owner = await api_user(client, db)
    video_id, parts = await _start_upload(client, owner, b"y" * 1000)
    vid = uuid.UUID(video_id)

    reads: list[set] = []
    real_job_ids = videos_repo.job_ids_of

    async def spy(session, video_id):
        result = await real_job_ids(session, video_id)
        reads.append(result)
        return result

    monkeypatch.setattr(videos_repo, "job_ids_of", spy)

    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as upload_db:
        upload_pid = await backend_pid(upload_db)
        real_commit = upload_db.commit

        async def gated_commit() -> None:
            holding.set()
            await release.wait()
            await real_commit()

        upload_db.commit = gated_commit  # type: ignore[method-assign]
        user = await upload_db.get(User, uuid.UUID(owner["id"]))
        complete = asyncio.create_task(uploads_service.complete_upload(upload_db, user, vid, parts))
        await asyncio.wait_for(holding.wait(), timeout=5)

        delete = asyncio.create_task(client.delete(f"/api/videos/{vid}", headers=owner["auth"]))
        await wait_for_lock_waiters(upload_pid)
        release.set()
        await complete
        r = await delete

    assert r.status_code == 204, r.text
    assert len(reads) == 2 and reads[0] and reads[0] == reads[1]  # первая попытка — повтор
    await _assert_gone(db, vid)


async def test_delete_gives_up_after_attempts(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Набор задач меняется на каждой попытке — после DELETE_ATTEMPTS ответ 503 с
    Retry-After, видео и файлы целы."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    calls: list[int] = []

    async def always_new(_session, _video_id):
        calls.append(1)
        return {uuid.uuid4()}

    monkeypatch.setattr(videos_repo, "job_ids_of", always_new)
    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 503, r.text
    assert r.headers["retry-after"] == "2"
    assert r.json()["detail"] == deletion.VIDEO_BUSY
    assert len(calls) == deletion.DELETE_ATTEMPTS
    assert await _rows(db, Video, video.id) == 1
    assert len(await _media_keys(video.id)) == 3


# --- Уборка сирот yt-raw --------------------------------------------------------------------


async def test_raw_sweep_removes_orphans_only(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Объект и незавершённая загрузка без строки видео и исходник готового видео — удаляются;
    исходник видео в обработке и чужие ключи — нет. Моложе ORPHAN_MEDIA_MINUTES — не трогается."""
    owner = await api_user(client, db)
    bucket = _settings().s3_bucket_raw
    s3 = storage.get_s3()
    gone_object, gone_upload = uuid.uuid4(), uuid.uuid4()
    await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=raw_key(gone_object), Body=b"o")
    await asyncio.to_thread(storage.create_multipart_upload, bucket, raw_key(gone_upload))
    foreign = f"raw/{uuid.uuid4()}/other"
    await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=foreign, Body=b"f")
    live = await seed_video(db, await channel_of(db, owner), PRIV, PROC)
    await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=raw_key(live.id), Body=b"l")
    done = await seed_video(db, await channel_of(db, owner), PRIV)
    await asyncio.to_thread(s3.put_object, Bucket=bucket, Key=raw_key(done.id), Body=b"d")

    # Порог не наступил — свежие сироты не удаляются (в бакете тестов могут быть старые
    # остатки прошлых прогонов, поэтому проверяем конкретные ключи, а не счётчик).
    await maintenance.sweep_orphan_raw()
    assert await _raw_exists(gone_object)
    assert len(await _uploads_of(gone_upload)) == 1

    monkeypatch.setattr(_settings(), "orphan_media_minutes", -1)
    assert await maintenance.sweep_orphan_raw() >= 3
    assert not await _raw_exists(gone_object)
    assert await _uploads_of(gone_upload) == []
    assert await _raw_exists(live.id)
    assert await asyncio.to_thread(storage.object_size_or_none, bucket, foreign) == 1
    assert not await _raw_exists(done.id)  # ready: исходник не нужен, worker не смог удалить


async def test_raw_sweep_keeps_long_upload_of_live_video(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Загрузка живого видео старше порога (долгая загрузка 2 ГБ) не обрывается: после
    уборки её можно завершить."""
    owner = await api_user(client, db)
    video_id, parts = await _start_upload(client, owner, b"z" * 1000)
    vid = uuid.UUID(video_id)
    monkeypatch.setattr(_settings(), "orphan_media_minutes", -1)  # загрузка «старая»

    await maintenance.sweep_orphan_raw()
    assert len(await _uploads_of(vid)) == 1
    r = await client.post(
        f"/api/uploads/{video_id}/complete",
        json={"parts": [{"number": n, "etag": e} for n, e in parts]},
        headers=owner["auth"],
    )
    assert r.status_code == 200, r.text
    assert await _raw_exists(vid)
