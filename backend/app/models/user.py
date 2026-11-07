import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAt, Timestamps, UUIDPk
from app.models.enums import UserRole, pg_enum


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("username ~ '^[A-Za-z0-9_]{3,30}$'", name="username_format"),
        CheckConstraint("position('@' in email) > 1", name="email_format"),
    )

    # citext: уникальность без учёта регистра (Foo@x.ru и foo@x.ru — один пользователь).
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False, server_default=UserRole.user.value
    )
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    banned_reason: Mapped[str | None] = mapped_column(Text)

    # passive_deletes: каскад делает БД (ON DELETE CASCADE). Без него ORM при удалении
    # пользователя пытается обнулить channels.owner_id и падает на NOT NULL.
    channel: Mapped["Channel"] = relationship(
        back_populates="owner", uselist=False, passive_deletes=True, lazy="raise"
    )


class Channel(UUIDPk, CreatedAt, Base):
    """Канал пользователя (1:1). Создаётся при регистрации."""

    __tablename__ = "channels"
    __table_args__ = (
        CheckConstraint("handle ~ '^[A-Za-z0-9_.-]{3,30}$'", name="handle_format"),
        CheckConstraint("char_length(btrim(title)) BETWEEN 1 AND 100", name="title_length"),
        CheckConstraint("char_length(description) <= 5000", name="description_length"),
        CheckConstraint("subscribers_count >= 0", name="subscribers_non_negative"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    handle: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    avatar_key: Mapped[str | None] = mapped_column(String(255))
    banner_key: Mapped[str | None] = mapped_column(String(255))
    # Денормализованный счётчик: обновляется в той же транзакции, что и подписка.
    subscribers_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    owner: Mapped[User] = relationship(back_populates="channel", lazy="raise")


class UserSession(UUIDPk, CreatedAt, Base):
    """Серверная сессия (ADR 0005). Токен в БД — только SHA-256, сам токен знает лишь клиент."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_sha256"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(300))
