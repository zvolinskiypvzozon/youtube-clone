"""SQL очереди задач (контракт — docstring модели app/models/job.py).

Все изменения задачи после захвата — только с fencing-токеном (locked_by): worker,
чей lease истёк и чью задачу перехватили, не перезапишет результат другого.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStatus, Video, VideoStatus


@dataclass(frozen=True)
class Claimed:
    job_id: uuid.UUID
    video_id: uuid.UUID
    token: uuid.UUID
    attempts: int
    max_attempts: int


def _seconds(n: float):
    return func.make_interval(0, 0, 0, 0, 0, 0, n)


async def claim(db: AsyncSession, lease_seconds: int) -> Claimed | None:
    """Захватывает одну готовую задачу. SKIP LOCKED: параллельные worker-ы не ждут друг
    друга и не берут одну задачу дважды. Новый fencing-токен на каждый захват."""
    picked = await db.scalar(
        select(Job.id)
        .where(
            Job.status == JobStatus.queued,
            Job.run_after <= func.now(),
            Job.attempts < Job.max_attempts,
        )
        .order_by(Job.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if picked is None:
        return None
    token = uuid.uuid4()
    row = (
        await db.execute(
            update(Job)
            .where(Job.id == picked)
            .values(
                status=JobStatus.running,
                attempts=Job.attempts + 1,
                locked_by=token,
                locked_until=func.now() + _seconds(lease_seconds),
            )
            .returning(Job.video_id, Job.attempts, Job.max_attempts)
        )
    ).one()
    return Claimed(picked, row.video_id, token, row.attempts, row.max_attempts)


def _owned(job_id: uuid.UUID, token: uuid.UUID):
    return and_(Job.id == job_id, Job.status == JobStatus.running, Job.locked_by == token)


async def extend_lease(
    db: AsyncSession, job_id: uuid.UUID, token: uuid.UUID, lease_seconds: int
) -> bool:
    """Heartbeat. False — задачу перехватили или удалили (видео удалено): работу прекратить."""
    result = await db.execute(
        update(Job)
        .where(_owned(job_id, token))
        .values(locked_until=func.now() + _seconds(lease_seconds))
        .returning(Job.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_done(db: AsyncSession, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    result = await db.execute(
        update(Job)
        .where(_owned(job_id, token))
        .values(status=JobStatus.done, locked_by=None, locked_until=None, last_error=None)
        .returning(Job.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_failed(db: AsyncSession, job_id: uuid.UUID, token: uuid.UUID, error: str) -> bool:
    result = await db.execute(
        update(Job)
        .where(_owned(job_id, token))
        .values(status=JobStatus.failed, locked_by=None, locked_until=None, last_error=error)
        .returning(Job.id)
    )
    return result.scalar_one_or_none() is not None


async def requeue(
    db: AsyncSession,
    job_id: uuid.UUID,
    token: uuid.UUID,
    error: str,
    delay_seconds: float,
    *,
    refund_attempt: bool = False,
) -> bool:
    """Вернуть задачу в очередь. refund_attempt — попытка не засчитывается (остановка
    worker при деплое — не вина файла; иначе несколько деплоев подряд «сожгли» бы попытки)."""
    result = await db.execute(
        update(Job)
        .where(_owned(job_id, token))
        .values(
            attempts=Job.attempts - 1 if refund_attempt else Job.attempts,
            status=JobStatus.queued,
            locked_by=None,
            locked_until=None,
            last_error=error,
            run_after=func.now() + _seconds(delay_seconds),
        )
        .returning(Job.id)
    )
    return result.scalar_one_or_none() is not None


async def reclaim_expired(db: AsyncSession) -> tuple[int, list[uuid.UUID]]:
    """Задачи упавших worker-ов (lease истёк). Два UPDATE (контракт модели):
    попытки остались → обратно в очередь; исчерпаны → failed. Возвращает (сколько вернули,
    видео исчерпанных задач) — видео переводит в failed вызывающий."""
    expired = and_(Job.status == JobStatus.running, Job.locked_until < func.now())
    requeued = await db.execute(
        update(Job)
        .where(expired, Job.attempts < Job.max_attempts)
        .values(
            status=JobStatus.queued,
            locked_by=None,
            locked_until=None,
            run_after=func.now(),
            last_error="lease истёк: worker не завершил задачу",
        )
        .returning(Job.id)
    )
    exhausted = await db.execute(
        update(Job)
        .where(expired, Job.attempts >= Job.max_attempts)
        .values(
            status=JobStatus.failed,
            locked_by=None,
            locked_until=None,
            last_error="lease истёк, попытки исчерпаны",
        )
        .returning(Job.video_id)
    )
    return len(requeued.all()), list(exhausted.scalars())


async def fail_videos(db: AsyncSession, video_ids: list[uuid.UUID], reason: str) -> None:
    if not video_ids:
        return
    await db.execute(
        update(Video)
        .where(Video.id.in_(video_ids), Video.status == VideoStatus.processing)
        .values(status=VideoStatus.failed, failure_reason=reason)
    )
