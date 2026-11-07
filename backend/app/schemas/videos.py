import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ModerationState, VideoStatus, VideoVisibility


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


class ChannelOut(BaseModel):
    id: uuid.UUID
    handle: str
    title: str
    description: str
    subscribers_count: int
    created_at: datetime
