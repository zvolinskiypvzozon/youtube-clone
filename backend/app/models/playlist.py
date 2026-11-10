import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Timestamps, UUIDPk
from app.models.enums import PlaylistVisibility, pg_enum


class Playlist(UUIDPk, Timestamps, Base):
    """Плейлист канала. В него попадают только видео этого же канала (решение архитектора
    2026-11-09 и 2026-11-10). Счётчика видео нет: число считается при запросе и только по видео,
    видимым текущему зрителю, — пересчитывать при бане и удалении пользователя нечего.
    Права — services/policies.py (can_view_playlist, listed_playlists_filter, can_manage);
    порядок блокировок — ADR 0006 доп. 7."""

    __tablename__ = "playlists"
    __table_args__ = (
        CheckConstraint("char_length(btrim(title)) BETWEEN 1 AND 150", name="title_length"),
        CheckConstraint("char_length(description) <= 5000", name="description_length"),
        # Плейлисты канала и «Мои плейлисты»: новые сверху, keyset по (created_at, id).
        Index(
            "ix_playlists_channel_created",
            "channel_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    visibility: Mapped[PlaylistVisibility] = mapped_column(
        pg_enum(PlaylistVisibility, "playlist_visibility"),
        nullable=False,
        server_default=PlaylistVisibility.private.value,
    )


class PlaylistItem(Base):
    """Видео в плейлисте: одно видео — один раз (PK), порядок — position (уникальна в плейлисте,
    после удаления остаются дыры). Позиция = max + 1 под блокировкой строки плейлиста."""

    __tablename__ = "playlist_items"
    __table_args__ = (
        CheckConstraint("position >= 1", name="position_positive"),
        # Порядок и keyset-пагинация состава.
        UniqueConstraint("playlist_id", "position", name="uq_playlist_items_playlist_position"),
        # Каскад при удалении видео: без индекса DELETE videos сканировал бы всю таблицу.
        Index("ix_playlist_items_video", "video_id"),
    )

    playlist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
