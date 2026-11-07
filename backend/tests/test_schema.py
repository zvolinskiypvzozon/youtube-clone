"""Ограничения целостности на уровне БД.

Они защищают данные даже при ошибке в коде приложения, поэтому тестируются отдельно,
под той же ролью yt_app, под которой работают api и worker.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Channel,
    Job,
    JobStatus,
    JobType,
    User,
    UserSession,
    Video,
    VideoAccess,
    VideoFile,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from tests.factories import create_user, create_video


async def _channel(db: AsyncSession, user: User) -> Channel:
    return (await db.execute(select(Channel).where(Channel.owner_id == user.id))).scalar_one()


async def test_email_unique_case_insensitive(db: AsyncSession) -> None:
    await create_user(db, email="Alice@Example.com")
    with pytest.raises(IntegrityError):
        await create_user(db, email="alice@example.COM")


async def test_username_unique_case_insensitive(db: AsyncSession) -> None:
    await create_user(db, username="Alice_1")
    with pytest.raises(IntegrityError):
        await create_user(db, username="alice_1")


@pytest.mark.parametrize("username", ["ab", "with space", "кириллица", "x" * 31, "semi;colon"])
async def test_username_format_enforced(db: AsyncSession, username: str) -> None:
    with pytest.raises(IntegrityError):
        await create_user(db, username=username, with_channel=False)


async def test_one_channel_per_user(db: AsyncSession) -> None:
    user = await create_user(db)
    db.add(Channel(owner_id=user.id, handle="second_handle", title="Второй"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_ready_private_video_may_stay_unpublished(db: AsyncSession) -> None:
    """published_at ставится при первой публикации (не приватно), а не при готовности."""
    channel = await _channel(db, await create_user(db))
    video = await create_video(
        db, channel, status=VideoStatus.ready, visibility=VideoVisibility.private
    )
    assert video.published_at is None


async def test_not_ready_video_cannot_be_published(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    db.add(
        Video(
            channel_id=channel.id,
            title="x",
            status=VideoStatus.processing,
            published_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_channel_title_cannot_be_blank(db: AsyncSession) -> None:
    user = await create_user(db, with_channel=False)
    db.add(Channel(owner_id=user.id, handle=user.username, title="   "))
    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.parametrize("title", ["", "   ", "x" * 101])
async def test_video_title_length(db: AsyncSession, title: str) -> None:
    channel = await _channel(db, await create_user(db))
    with pytest.raises((IntegrityError, DBAPIError)):
        await create_video(db, channel, title=title)


async def test_video_size_limit_2gb(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel, status=VideoStatus.uploading)
    video.size_bytes = 2 * 1024**3 + 1
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_video_duration_limit_60_minutes(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel)
    video.duration_seconds = 3600
    await db.flush()  # ровно 60 минут — можно
    video.duration_seconds = 3601
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_updated_at_readable_after_update(db: AsyncSession) -> None:
    """eager_defaults: updated_at приходит через RETURNING, чтение не делает скрытый запрос
    (в async скрытый запрос = MissingGreenlet = 500 в API)."""
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel)
    before = video.updated_at
    await db.execute(text("SELECT pg_sleep(0.01)"))
    video.title = "Новое название"
    await db.flush()
    assert video.updated_at >= before  # без eager_defaults здесь был бы MissingGreenlet


async def test_counters_cannot_go_negative(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel)
    video.likes_count = -1
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_single_active_job_per_video(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel, status=VideoStatus.processing)
    first = Job(type=JobType.process_video, video_id=video.id)
    db.add(first)
    await db.flush()

    db.add(Job(type=JobType.process_video, video_id=video.id))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_new_job_allowed_after_previous_finished(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel, status=VideoStatus.processing)
    first = Job(type=JobType.process_video, video_id=video.id)
    db.add(first)
    await db.flush()
    first.status = JobStatus.failed
    await db.flush()

    db.add(Job(type=JobType.process_video, video_id=video.id))
    await db.flush()  # повторная обработка после неудачи разрешена


@pytest.mark.parametrize("max_attempts", [0, 11])
async def test_job_max_attempts_range(db: AsyncSession, max_attempts: int) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel, status=VideoStatus.processing)
    db.add(Job(type=JobType.process_video, video_id=video.id, max_attempts=max_attempts))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_running_job_requires_lease(db: AsyncSession) -> None:
    """running ⇔ есть fencing-токен и срок lease: без них нельзя отличить «свой» захват."""
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel, status=VideoStatus.processing)
    job = Job(type=JobType.process_video, video_id=video.id)
    db.add(job)
    await db.flush()

    job.status = JobStatus.running
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_job_claim_with_lease_ok(db: AsyncSession) -> None:
    channel = await _channel(db, await create_user(db))
    video = await create_video(db, channel, status=VideoStatus.processing)
    job = Job(type=JobType.process_video, video_id=video.id)
    db.add(job)
    await db.flush()

    job.status = JobStatus.running
    job.attempts = 1
    job.locked_by = uuid.uuid4()
    job.locked_until = datetime.now(UTC) + timedelta(minutes=10)
    await db.flush()


async def test_session_token_hash_must_be_sha256(db: AsyncSession) -> None:
    user = await create_user(db)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=b"short",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_deleting_user_cascades(db: AsyncSession) -> None:
    owner = await create_user(db)
    viewer = await create_user(db, with_channel=False)
    video = await create_video(db, await _channel(db, owner))
    db.add(VideoAccess(video_id=video.id, user_id=viewer.id))
    db.add(Job(type=JobType.process_video, video_id=video.id))
    db.add(
        UserSession(
            user_id=owner.id,
            token_hash=b"\x01" * 32,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db.flush()

    await db.delete(owner)
    await db.flush()

    for model in (Channel, Video, Job, UserSession, VideoAccess):
        count = (await db.execute(select(func.count()).select_from(model))).scalar_one()
        assert count == 0, f"{model.__tablename__}: остались записи после удаления владельца"
    # Пользователь из списка допуска не затронут.
    assert await db.get(User, viewer.id) is not None


async def test_deleting_video_cascades(db: AsyncSession) -> None:
    owner = await create_user(db)
    viewer = await create_user(db, with_channel=False)
    video = await create_video(db, await _channel(db, owner))
    db.add(VideoAccess(video_id=video.id, user_id=viewer.id))
    db.add(Job(type=JobType.process_video, video_id=video.id))
    db.add(
        VideoFile(
            video_id=video.id,
            kind=VideoFileKind.thumbnail,
            bucket="yt-media",
            s3_key=f"videos/{video.id}/thumb.jpg",
            mime="image/jpeg",
        )
    )
    await db.flush()

    await db.delete(video)
    await db.flush()

    for model in (Job, VideoAccess, VideoFile):
        count = (await db.execute(select(func.count()).select_from(model))).scalar_one()
        assert count == 0, f"{model.__tablename__}: остались записи после удаления видео"
    assert await db.get(User, owner.id) is not None


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE hack (x int)",
        "DROP TABLE users",
        "ALTER TABLE users ADD COLUMN x int",
        "TRUNCATE users",
        "COPY (SELECT 1) TO PROGRAM 'id'",
    ],
)
async def test_app_role_cannot_change_schema(db: AsyncSession, ddl: str) -> None:
    """Роль приложения: только DML. Даже при SQL-инъекции схема и ОС недоступны."""
    with pytest.raises(DBAPIError, match="permission denied|must be owner|privilege"):
        await db.execute(text(ddl))
