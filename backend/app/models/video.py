import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import (
    ModerationState,
    ReactionKind,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
    pg_enum,
)


class Video(UUIDPk, Timestamps, Base):
    """Метаданные видео. Бинарные данные — только в хранилище (video_files.s3_key)."""

    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint("char_length(btrim(title)) BETWEEN 1 AND 100", name="title_length"),
        CheckConstraint("char_length(description) <= 5000", name="description_length"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds BETWEEN 0 AND 3600",
            name="duration_max_60min",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR (size_bytes > 0 AND size_bytes <= 2147483648)",
            name="size_limit_2gb",
        ),
        CheckConstraint(
            "views_count >= 0 AND likes_count >= 0 AND dislikes_count >= 0 AND comments_count >= 0",
            name="counters_non_negative",
        ),
        # published_at — момент первой публикации (готово и не приватно); дальше не меняется.
        CheckConstraint("published_at IS NULL OR status = 'ready'", name="published_only_if_ready"),
        # Лента главной: только готовые публичные не скрытые, новые сверху.
        # id — tie-breaker для стабильной keyset-пагинации (published_at, id) < (…).
        Index(
            "ix_videos_feed",
            text("published_at DESC"),
            text("id DESC"),
            postgresql_where=text(
                "status = 'ready' AND visibility = 'public' AND moderation = 'ok'"
            ),
        ),
        Index(
            "ix_videos_channel_published",
            "channel_id",
            text("published_at DESC"),
            text("id DESC"),
        ),
        # «Мои видео»: все видео канала в любом статусе, новые загрузки сверху.
        Index(
            "ix_videos_channel_created",
            "channel_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # Поиск по названию: триграммы (подстрока, без учёта регистра, опечатки).
        Index(
            "ix_videos_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    visibility: Mapped[VideoVisibility] = mapped_column(
        pg_enum(VideoVisibility, "video_visibility"),
        nullable=False,
        server_default=VideoVisibility.private.value,
    )
    status: Mapped[VideoStatus] = mapped_column(
        pg_enum(VideoStatus, "video_status"),
        nullable=False,
        server_default=VideoStatus.uploading.value,
    )
    moderation: Mapped[ModerationState] = mapped_column(
        pg_enum(ModerationState, "moderation_state"),
        nullable=False,
        server_default=ModerationState.ok.value,
    )
    moderation_reason: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    # Заявленный при загрузке размер; фактический сверяется при complete загрузки.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # Идентификатор S3 multipart upload, пока статус uploading.
    upload_id: Mapped[str | None] = mapped_column(String(255))

    views_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    likes_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dislikes_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    comments_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    files: Mapped[list["VideoFile"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


class VideoFile(UUIDPk, Base):
    """Объект в хранилище, относящийся к видео (исходник, транскоды, превью)."""

    __tablename__ = "video_files"
    __table_args__ = (
        UniqueConstraint("video_id", "kind", name="uq_video_files_video_kind"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_non_negative"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[VideoFileKind] = mapped_column(
        pg_enum(VideoFileKind, "video_file_kind"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    video: Mapped[Video] = relationship(back_populates="files", lazy="raise")


class VideoAccess(Base):
    """Список допуска к приватному видео: кому владелец разрешил просмотр."""

    __tablename__ = "video_access"
    # «Доступные мне»: по пользователю, новые выдачи сверху.
    __table_args__ = (Index("ix_video_access_user_granted", "user_id", text("granted_at DESC")),)

    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Reaction(Base):
    """Лайк или дизлайк пользователя к видео: одна строка на пару (видео, пользователь),
    поэтому лайк и дизлайк взаимоисключающие. Счётчики videos.likes_count/dislikes_count
    меняются в той же транзакции под блокировкой строки видео (services/reactions.py)."""

    __tablename__ = "reactions"
    # «Понравившиеся» (день 9: user_id, kind='like', новые сверху) и каскад при удалении
    # пользователя (по user_id).
    __table_args__ = (
        Index("ix_reactions_user_kind_created", "user_id", "kind", text("created_at DESC")),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[ReactionKind] = mapped_column(
        pg_enum(ReactionKind, "reaction_kind"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
