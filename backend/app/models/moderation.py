import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UUIDPk
from app.models.enums import ModerationAction, ReportCategory, ReportStatus, pg_enum


class Report(UUIDPk, CreatedAt, Base):
    """Жалоба на видео. Видна только staff: владелец видео не узнаёт, кто пожаловался.

    Снимок (название, канал) хранится в самой жалобе: при удалении видео video_id
    обнуляется (SET NULL), а история модерации остаётся. Порядок блокировок — «видео →
    жалобы» (ADR 0006 доп. 6).
    """

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("char_length(comment) <= 500", name="comment_length"),
        # Закрытая жалоба всегда с моментом закрытия, открытая — без.
        CheckConstraint("(status = 'open') = (resolved_at IS NULL)", name="resolved_consistent"),
        # Одна открытая жалоба от пользователя на видео. Дубли ловит индекс, а не проверка
        # в коде; он же — поиск открытых жалоб видео при скрытии.
        Index(
            "uq_reports_open_video_reporter",
            "video_id",
            "reporter_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        # Очередь модерации: по статусу, новые сверху, keyset по (created_at, id).
        Index("ix_reports_status_created", "status", text("created_at DESC"), text("id DESC")),
        # SET NULL при удалении видео затрагивает и закрытые жалобы — частичного индекса
        # выше для этого мало.
        Index("ix_reports_video", "video_id"),
        # SET NULL при удалении пользователя (день 9).
        Index("ix_reports_reporter", "reporter_id"),
        # Очередь модератора: жалобы на удалённые видео своего канала — по снимку channel_id.
        Index("ix_reports_channel", "channel_id"),
        # Потолок открытых жалоб пользователя (шаг 2 дня 6): подсчёт читает не больше 20 строк,
        # а не всю историю жалоб (ревью шага 2).
        Index(
            "ix_reports_reporter_open",
            "reporter_id",
            postgresql_where=text("status = 'open'"),
        ),
    )

    video_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    category: Mapped[ReportCategory] = mapped_column(
        pg_enum(ReportCategory, "report_category"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text)
    video_title: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_handle: Mapped[str] = mapped_column(String(30), nullable=False)
    # Снимок канала видео на момент жалобы — без внешнего ключа (переживает удаление видео и
    # пользователя). «Своё видео» модератора для удалённого видео — по нему, а не по handle:
    # ник удалённого пользователя навсегда занят, но сравнение по id не зависит и от этого
    # (ADR 0006 доп. 6, день 9). NULL — только у жалоб, чей канал уже удалён до миграции.
    channel_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[ReportStatus] = mapped_column(
        pg_enum(ReportStatus, "report_status"),
        nullable=False,
        server_default=ReportStatus.open.value,
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModerationLogEntry(UUIDPk, CreatedAt, Base):
    """Журнал действий staff. Цель — без внешнего ключа: запись переживает удаление видео,
    комментария или пользователя; что это было — в снимке `details`."""

    __tablename__ = "moderation_log"
    __table_args__ = (
        CheckConstraint("char_length(reason) <= 500", name="reason_length"),
        # История конкретной цели (видео, пользователь), новые сверху.
        Index("ix_moderation_log_target_created", "target_id", text("created_at DESC")),
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[ModerationAction] = mapped_column(
        pg_enum(ModerationAction, "moderation_action"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
