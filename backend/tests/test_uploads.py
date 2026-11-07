"""Загрузка видео: multipart напрямую в хранилище (реальные presigned PUT в yt-test-raw)."""

import asyncio
import os
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.errors import BadRequest, Conflict
from app.models import (
    Job,
    JobStatus,
    User,
    Video,
    VideoFile,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from app.repositories import videos as videos_repo
from app.schemas.uploads import UploadCreateIn
from app.services import uploads as uploads_service
from app.services.uploads import raw_key
from tests.helpers import new_user

MiB = 1024**2


def _meta(size: int, **overrides: object) -> dict:
    data = {
        "title": "Тестовое видео",
        "description": "",
        "visibility": "public",
        "content_type": "video/mp4",
        "size_bytes": size,
    }
    data.update(overrides)
    return data


async def _start(client: AsyncClient, auth: dict, size: int, **overrides: object) -> dict:
    r = await client.post("/api/uploads", json=_meta(size, **overrides), headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def _put_parts(upload: dict, payload: bytes) -> list[dict]:
    """Грузит части напрямую в хранилище, как это делает браузер."""
    size = upload["part_size"]
    done = []
    async with httpx.AsyncClient(timeout=60) as s3:
        for part in upload["parts"]:
            n = part["number"]
            chunk = payload[(n - 1) * size : n * size]
            r = await s3.put(part["url"], content=chunk)
            assert r.status_code == 200, r.text
            done.append({"number": n, "etag": r.headers["etag"]})
    return done


async def _complete(client: AsyncClient, auth: dict, video_id: str, parts: list[dict]):
    return await client.post(
        f"/api/uploads/{video_id}/complete", json={"parts": parts}, headers=auth
    )


# --- начало загрузки -------------------------------------------------------------------


async def test_start_upload_returns_signed_part_urls(client: AsyncClient, db: AsyncSession) -> None:
    user = await new_user(client)
    upload = await _start(client, user["auth"], 33 * MiB)  # 16 + 16 + 1

    assert upload["part_size"] == 16 * MiB
    assert [p["number"] for p in upload["parts"]] == [1, 2, 3]
    url = upload["parts"][0]["url"]
    assert f"/{get_settings().s3_bucket_raw}/raw/{upload['video_id']}/source" in url
    assert "X-Amz-Signature=" in url and "uploadId=" in url

    video = await db.get(Video, uuid.UUID(upload["video_id"]))
    assert video.status == VideoStatus.uploading
    assert video.size_bytes == 33 * MiB
    assert video.upload_id


async def test_start_upload_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/api/uploads", json=_meta(1000))
    assert r.status_code == 401


@pytest.mark.parametrize(
    "override",
    [
        {"size_bytes": 2 * 1024**3 + 1},
        {"size_bytes": 0},
        {"content_type": "text/html"},
        {"content_type": "application/octet-stream"},
        {"title": "   "},
        {"title": "x" * 101},
        {"visibility": "secret"},
    ],
)
async def test_start_upload_validation(client: AsyncClient, override: dict) -> None:
    user = await new_user(client)
    r = await client.post("/api/uploads", json=_meta(1000, **override), headers=user["auth"])
    assert r.status_code == 422


async def test_visibility_is_required(client: AsyncClient) -> None:
    user = await new_user(client)
    data = _meta(1000)
    del data["visibility"]
    r = await client.post("/api/uploads", json=data, headers=user["auth"])
    assert r.status_code == 422


async def test_active_uploads_limit(client: AsyncClient) -> None:
    user = await new_user(client)
    for _ in range(get_settings().max_active_uploads):
        await _start(client, user["auth"], 1000)
    r = await client.post("/api/uploads", json=_meta(1000), headers=user["auth"])
    assert r.status_code == 409


async def test_start_upload_via_cookie_requires_csrf(client: AsyncClient) -> None:
    s = uuid.uuid4().hex[:8]
    await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    assert (await client.post("/api/uploads", json=_meta(1000))).status_code == 403
    r = await client.post(
        "/api/uploads", json=_meta(1000), headers={"X-CSRF-Token": client.cookies["csrf"]}
    )
    assert r.status_code == 201


# --- завершение ------------------------------------------------------------------------


async def test_full_upload_cycle_enqueues_processing(client: AsyncClient, db: AsyncSession) -> None:
    user = await new_user(client)
    payload = os.urandom(17 * MiB)  # две части: 16 МиБ + 1 МиБ
    upload = await _start(client, user["auth"], len(payload))
    parts = await _put_parts(upload, payload)

    r = await _complete(client, user["auth"], upload["video_id"], parts)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "processing"

    vid = uuid.UUID(upload["video_id"])
    video = await db.get(Video, vid)
    assert video.status == VideoStatus.processing and video.upload_id is None
    original = (await db.execute(select(VideoFile).where(VideoFile.video_id == vid))).scalar_one()
    assert original.kind == VideoFileKind.original and original.size_bytes == len(payload)
    job = (await db.execute(select(Job).where(Job.video_id == vid))).scalar_one()
    assert job.status == JobStatus.queued

    size = await asyncio.to_thread(storage.object_size, get_settings().s3_bucket_raw, raw_key(vid))
    assert size == len(payload)


async def test_complete_twice_conflicts_and_single_job(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = await _put_parts(upload, payload)

    assert (await _complete(client, user["auth"], upload["video_id"], parts)).status_code == 200
    assert (await _complete(client, user["auth"], upload["video_id"], parts)).status_code == 409
    jobs = await db.scalar(
        select(func.count()).select_from(Job).where(Job.video_id == uuid.UUID(upload["video_id"]))
    )
    assert jobs == 1


async def test_concurrent_complete_is_serialized_by_row_lock(client: AsyncClient) -> None:
    """Детерминированная гонка двух complete: T1 держит блокировку строки видео (FOR UPDATE)
    и ещё не закоммитила перевод в processing. T2 обязана ждать, а затем получить 409.
    Без блокировки T2 прочитала бы статус uploading и пошла бы завершать загрузку второй раз."""
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = [(p["number"], p["etag"]) for p in await _put_parts(upload, payload)]
    vid, owner_id = uuid.UUID(upload["video_id"]), uuid.UUID(user["id"])

    maker = get_sessionmaker()
    async with maker() as t1, maker() as t2:
        owner = await t2.get(User, owner_id)
        await t2.commit()

        locked = await videos_repo.get_owned_for_update(t1, vid, owner_id)
        assert locked is not None and locked.status == VideoStatus.uploading

        t2_task = asyncio.create_task(uploads_service.complete_upload(t2, owner, vid, parts))
        await asyncio.sleep(0.5)
        assert not t2_task.done(), "второй complete должен ждать блокировку строки видео"

        locked.status = VideoStatus.processing
        locked.upload_id = None
        await t1.commit()
        with pytest.raises(Conflict):
            await t2_task


async def test_other_user_cannot_complete_or_cancel(client: AsyncClient) -> None:
    owner = await new_user(client)
    stranger = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, owner["auth"], len(payload))
    parts = await _put_parts(upload, payload)

    r = await _complete(client, stranger["auth"], upload["video_id"], parts)
    assert r.status_code == 404
    r = await client.delete(f"/api/uploads/{upload['video_id']}", headers=stranger["auth"])
    assert r.status_code == 404
    # Владелец по-прежнему может завершить.
    r = await _complete(client, owner["auth"], upload["video_id"], parts)
    assert r.status_code == 200


async def test_part_of_wrong_length_rejected_by_storage(client: AsyncClient) -> None:
    """Длина части входит в подпись ссылки: больше или меньше заявленного не загрузить.
    Это закрывает DoS хранилища «заявить 16 МиБ на часть, залить 128 МиБ»."""
    user = await new_user(client)
    upload = await _start(client, user["auth"], 2000)
    url = upload["parts"][0]["url"]
    assert upload["parts"][0]["size"] == 2000
    async with httpx.AsyncClient(timeout=30) as s3:
        assert (await s3.put(url, content=os.urandom(5000))).status_code == 403
        assert (await s3.put(url, content=os.urandom(1999))).status_code == 403
        assert (await s3.put(url, content=os.urandom(2000))).status_code == 200


async def test_size_mismatch_defense_marks_failed_and_removes_object(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вторая линия защиты: если склеенный объект всё же не совпал по размеру с заявленным,
    видео → failed, объект удаляется, задача не ставится."""
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = await _put_parts(upload, payload)
    real_size = storage.object_size
    monkeypatch.setattr(storage, "object_size", lambda b, k: real_size(b, k) + 1)

    r = await _complete(client, user["auth"], upload["video_id"], parts)
    assert r.status_code == 400
    vid = uuid.UUID(upload["video_id"])
    video = await db.get(Video, vid)
    assert video.status == VideoStatus.failed and video.failure_reason
    bucket = get_settings().s3_bucket_raw
    assert await asyncio.to_thread(storage.object_size_or_none, bucket, raw_key(vid)) is None
    assert await db.scalar(select(func.count()).select_from(Job).where(Job.video_id == vid)) == 0


async def test_complete_recovers_after_crash_between_assemble_and_commit(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Части склеены, но до commit процесс упал. Повторный complete не должен навсегда
    застревать на NoSuchUpload: объект уже есть — доводим до processing."""
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = [(p["number"], p["etag"]) for p in await _put_parts(upload, payload)]
    vid, owner = uuid.UUID(upload["video_id"]), await db.get(User, uuid.UUID(user["id"]))
    await db.commit()

    def crash(*_: object) -> int:
        raise RuntimeError("процесс упал после склейки")

    real_size = storage.object_size
    monkeypatch.setattr(storage, "object_size", crash)
    async with get_sessionmaker()() as s1:
        with pytest.raises(RuntimeError):
            await uploads_service.complete_upload(s1, owner, vid, parts)
    monkeypatch.setattr(storage, "object_size", real_size)

    async with get_sessionmaker()() as s2:
        video = await uploads_service.complete_upload(s2, owner, vid, parts)
        assert video.status == VideoStatus.processing


async def test_cancel_after_crash_removes_assembled_object(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = [(p["number"], p["etag"]) for p in await _put_parts(upload, payload)]
    vid, owner = uuid.UUID(upload["video_id"]), await db.get(User, uuid.UUID(user["id"]))
    await db.commit()

    def crash(*_: object) -> int:
        raise RuntimeError("процесс упал после склейки")

    monkeypatch.setattr(storage, "object_size", crash)
    async with get_sessionmaker()() as s1:
        with pytest.raises(RuntimeError):
            await uploads_service.complete_upload(s1, owner, vid, parts)
    monkeypatch.undo()

    r = await client.delete(f"/api/uploads/{vid}", headers=user["auth"])
    assert r.status_code == 204
    bucket = get_settings().s3_bucket_raw
    assert await asyncio.to_thread(storage.object_size_or_none, bucket, raw_key(vid)) is None


async def test_complete_with_missing_part_rejected(client: AsyncClient) -> None:
    user = await new_user(client)
    payload = os.urandom(17 * MiB)
    upload = await _start(client, user["auth"], len(payload))
    parts = await _put_parts(upload, payload)

    r = await _complete(client, user["auth"], upload["video_id"], parts[:1])
    assert r.status_code == 400
    # Загрузка не потеряна: с полным списком частей можно завершить.
    r = await _complete(client, user["auth"], upload["video_id"], parts)
    assert r.status_code == 200


async def test_complete_with_wrong_etag_rejected(client: AsyncClient) -> None:
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = await _put_parts(upload, payload)
    parts[0]["etag"] = '"00000000000000000000000000000000"'
    r = await _complete(client, user["auth"], upload["video_id"], parts)
    assert r.status_code == 400


async def test_complete_unknown_upload(client: AsyncClient) -> None:
    user = await new_user(client)
    r = await _complete(client, user["auth"], str(uuid.uuid4()), [{"number": 1, "etag": "x"}])
    assert r.status_code == 404


# --- отмена ----------------------------------------------------------------------------


async def test_cancel_upload_removes_video(client: AsyncClient, db: AsyncSession) -> None:
    user = await new_user(client)
    upload = await _start(client, user["auth"], 1000)
    r = await client.delete(f"/api/uploads/{upload['video_id']}", headers=user["auth"])
    assert r.status_code == 204
    assert await db.get(Video, uuid.UUID(upload["video_id"])) is None
    # multipart отменён в хранилище: ссылка на часть больше не работает.
    async with httpx.AsyncClient(timeout=30) as s3:
        put = await s3.put(upload["parts"][0]["url"], content=os.urandom(1000))
    assert put.status_code == 404
    # Слот в лимите незавершённых загрузок освобождён.
    for _ in range(get_settings().max_active_uploads):
        await _start(client, user["auth"], 1000)


async def test_cannot_cancel_completed_upload(client: AsyncClient) -> None:
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    await _complete(client, user["auth"], upload["video_id"], await _put_parts(upload, payload))
    r = await client.delete(f"/api/uploads/{upload['video_id']}", headers=user["auth"])
    assert r.status_code == 409


# --- подпись URL -----------------------------------------------------------------------


def test_presigned_host_follows_request_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без S3_PUBLIC_ENDPOINT URL подписывается на origin запроса (nginx, ADR 0004)."""
    from starlette.requests import Request

    from app.api.routes.uploads import public_storage_endpoint

    monkeypatch.setattr(get_settings(), "s3_public_endpoint", None)
    scope = {
        "type": "http",
        "scheme": "http",
        "method": "POST",
        "path": "/api/uploads",
        "headers": [(b"host", b"192.168.1.50:8080")],
        "server": ("api", 8000),
        "query_string": b"",
    }
    assert public_storage_endpoint(Request(scope)) == "http://192.168.1.50:8080"


async def test_concurrent_starts_respect_limit(client: AsyncClient) -> None:
    """Детерминированная гонка лимита незавершённых загрузок.

    У пользователя лимит − 1 загрузок. T1 (параллельный старт) взяла блокировку канала
    и создала последнюю разрешённую загрузку, но ещё не закоммитила. Старт в T2 обязан
    дождаться T1 и получить 409. Без блокировки T2 посчитала бы загрузки до commit T1
    (видно лимит − 1) и создала бы лишнюю."""
    user = await new_user(client)
    limit = get_settings().max_active_uploads
    for _ in range(limit - 1):
        await _start(client, user["auth"], 1000)

    owner_id = uuid.UUID(user["id"])
    maker = get_sessionmaker()
    async with maker() as t1, maker() as t2:
        owner = await t2.get(User, owner_id)
        await t2.commit()
        channel_id = await videos_repo.channel_id_of(t1, owner_id)
        await videos_repo.lock_channel(t1, channel_id)
        await videos_repo.create_uploading(
            t1,
            channel_id=channel_id,
            title="параллельный старт",
            description="",
            visibility=VideoVisibility.public,
            size_bytes=1000,
        )

        data = UploadCreateIn(**_meta(1000))
        t2_task = asyncio.create_task(
            uploads_service.start_upload(t2, owner, data, "http://storage:9000")
        )
        await asyncio.sleep(0.5)
        await t1.commit()
        with pytest.raises(Conflict):
            await t2_task


async def test_processing_and_failed_do_not_occupy_upload_slots(client: AsyncClient) -> None:
    user = await new_user(client)
    payload = os.urandom(1000)
    upload = await _start(client, user["auth"], len(payload))
    parts = await _put_parts(upload, payload)
    assert (await _complete(client, user["auth"], upload["video_id"], parts)).status_code == 200
    for _ in range(get_settings().max_active_uploads):  # processing слот не занимает
        await _start(client, user["auth"], 1000)


@pytest.mark.parametrize("host", ["bad host", "x:99999999", "a/b", ""])
def test_invalid_host_rejected_before_signing(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.requests import Request

    from app.api.routes.uploads import public_storage_endpoint

    monkeypatch.setattr(get_settings(), "s3_public_endpoint", None)
    scope = {
        "type": "http",
        "scheme": "http",
        "method": "POST",
        "path": "/api/uploads",
        "headers": [(b"host", host.encode())],
        "server": ("api", 8000),
        "query_string": b"",
    }
    with pytest.raises(BadRequest):
        public_storage_endpoint(Request(scope))
