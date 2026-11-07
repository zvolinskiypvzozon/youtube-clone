import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import JobStatus, JobType, pg_enum


class Job(UUIDPk, Timestamps, Base):
    """Задача фоновой обработки (ADR 0003). Контракт очереди:

    Захват (одна транзакция):
        SELECT ... WHERE status='queued' AND run_after <= now()
        ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1;
        UPDATE SET status='running', attempts = attempts + 1,
                   locked_by = <новый uuid>, locked_until = now() + lease.

    Завершение / ошибка — только с fencing-токеном:
        UPDATE ... WHERE id = :id AND status = 'running' AND locked_by = :token.
        0 строк → задачу уже перехватил другой worker (наш lease истёк): результат отбрасываем.

    Возврат задач упавшего worker (lease истёк):
        attempts < max_attempts  → status='queued', locked_by=NULL, run_after = now()
                                   (сам факт падения worker-а — не повод для паузы);
        attempts >= max_attempts → status='failed' (и видео → failed) — отдельным UPDATE,
        иначе CHECK attempts_range не даст вернуть задачу в очередь.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_range"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="max_attempts_range"),
        CheckConstraint(
            "(status = 'running') = (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="lease_iff_running",
        ),
        # Выбор следующей задачи.
        Index("ix_jobs_queued", "run_after", postgresql_where=text("status = 'queued'")),
        # Поиск задач с истёкшим lease.
        Index("ix_jobs_running_lease", "locked_until", postgresql_where=text("status = 'running'")),
        # FK для каскадного удаления видео: done/failed-задачи копятся, без индекса — seq scan.
        Index("ix_jobs_video_id", "video_id"),
        # Не более одной активной задачи одного типа на видео: повторный complete загрузки
        # не поставит дубль. (От двойного *завершения* защищает fencing по locked_by.)
        Index(
            "uq_jobs_active_per_video",
            "video_id",
            "type",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    type: Mapped[JobType] = mapped_column(pg_enum(JobType, "job_type"), nullable=False)
    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), nullable=False, server_default=JobStatus.queued.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Fencing-токен текущего захвата: новый uuid при каждом захвате задачи worker-ом.
    locked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
