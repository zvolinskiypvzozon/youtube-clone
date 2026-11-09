import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import ModerationState, ReactionKind, VideoStatus, VideoVisibility
from app.schemas.uploads import Title


class ChannelBrief(BaseModel):
    id: uuid.UUID
    handle: str
    title: str


class VideoStateOut(BaseModel):
    """Служебное состояние видео — только владельцу и модераторам/админам."""

    status: VideoStatus
    visibility: VideoVisibility
    moderation: ModerationState
    moderation_reason: str | None
    failure_reason: str | None


class VideoCardOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    channel: ChannelBrief
    duration_seconds: int | None
    views_count: int
    likes_count: int
    dislikes_count: int
    comments_count: int
    published_at: datetime | None
    thumbnail_url: str | None
    my_reaction: ReactionKind | None = Field(
        default=None, description="Реакция текущего пользователя; null — нет или аноним"
    )
    state: VideoStateOut | None = Field(
        description="Только владельцу и модераторам/админам; остальным — null"
    )


class VideoSourceOut(BaseModel):
    quality: str = Field(examples=["720p"])
    height: int | None
    mime: str
    size_bytes: int | None
    url: str


class VideoStreamOut(BaseModel):
    video_id: uuid.UUID
    sources: list[VideoSourceOut] = Field(description="Лучшее качество первым")
    thumbnail_url: str | None
    expires_in: int = Field(
        description="Срок действия ссылок, секунд. После истечения — запросить заново"
    )


class VideoListItem(BaseModel):
    id: uuid.UUID
    title: str
    channel: ChannelBrief
    duration_seconds: int | None
    views_count: int
    published_at: datetime
    thumbnail_url: str | None


class VideoPage(BaseModel):
    items: list[VideoListItem]
    next_cursor: str | None = Field(description="null — это последняя страница")


class MyVideoItem(BaseModel):
    """Своё видео в «Моих видео»: с служебным состоянием, в любом статусе."""

    id: uuid.UUID
    title: str
    duration_seconds: int | None
    views_count: int
    created_at: datetime
    published_at: datetime | None = Field(description="null — ещё не публиковалось")
    thumbnail_url: str | None
    state: VideoStateOut


class MyVideoPage(BaseModel):
    items: list[MyVideoItem]
    next_cursor: str | None = Field(description="null — это последняя страница")


class ChannelOut(BaseModel):
    id: uuid.UUID
    handle: str
    title: str
    description: str
    subscribers_count: int
    created_at: datetime


class VideoUpdateIn(BaseModel):
    """Правка владельцем: передаются только меняемые поля, хотя бы одно. null — не значение."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = Field(default=None, examples=["Новое название"])
    description: str | None = Field(default=None, max_length=5000)
    visibility: VideoVisibility | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> "VideoUpdateIn":
        if not self.model_fields_set:
            raise ValueError("Нужно передать хотя бы одно поле")
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"Поля не могут быть null: {', '.join(nulls)}")
        return self


class AccessGrantIn(BaseModel):
    """Кому открыть видео: по нику (ники публичны; поиск по email раскрывал бы,
    какие адреса зарегистрированы)."""

    model_config = ConfigDict(extra="forbid")

    # Формат — как CHECK username_format: заведомо невозможный ник (пробелы, NUL, кириллица)
    # отсекается до БД (NUL в text Postgres не принимает — было бы 500, аудит 3b).
    username: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_]{3,30}$")
    ] = Field(examples=["alice_42"])


class AccessEntryOut(BaseModel):
    user_id: uuid.UUID
    username: str
    granted_at: datetime


class AccessListOut(BaseModel):
    items: list[AccessEntryOut] = Field(description="Новые выдачи сверху")
    max_entries: int = Field(description="Лимит записей на одно видео")


class ReactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ReactionKind


class ReactionOut(BaseModel):
    my_reaction: ReactionKind | None
    likes_count: int
    dislikes_count: int
