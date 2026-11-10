import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.models import PlaylistVisibility
from app.schemas.common import no_nul
from app.schemas.videos import ChannelBrief, VideoStateOut

PLAYLIST_TITLE_MAX = 150
PLAYLIST_DESCRIPTION_MAX = 5000

# Длина — после обрезки пробелов по краям, как в CHECK title_length.
PlaylistTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=PLAYLIST_TITLE_MAX),
    AfterValidator(no_nul),
]
PlaylistDescription = Annotated[
    str, StringConstraints(max_length=PLAYLIST_DESCRIPTION_MAX), AfterValidator(no_nul)
]


class PlaylistCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: PlaylistTitle = Field(examples=["Лучшее за год"])
    description: PlaylistDescription = ""
    # Обязательное поле, как у загрузки видео: API не угадывает видимость.
    visibility: PlaylistVisibility


class PlaylistUpdateIn(BaseModel):
    """Правка владельцем: передаются только меняемые поля, хотя бы одно. null — не значение."""

    model_config = ConfigDict(extra="forbid")

    title: PlaylistTitle | None = None
    description: PlaylistDescription | None = None
    visibility: PlaylistVisibility | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> "PlaylistUpdateIn":
        if not self.model_fields_set:
            raise ValueError("Нужно передать хотя бы одно поле")
        nulls = sorted(f for f in self.model_fields_set if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"Поля не могут быть null: {', '.join(nulls)}")
        return self


_COUNT_DESCRIPTION = "Число видео, которые может открыть текущий зритель"
_THUMB_DESCRIPTION = "Превью первого видео, которое может открыть текущий зритель"
_VISIBILITY_DESCRIPTION = "Только владельцу; остальным — null"


class PlaylistOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    channel: ChannelBrief
    video_count: int = Field(description=_COUNT_DESCRIPTION)
    thumbnail_url: str | None = Field(description=_THUMB_DESCRIPTION)
    visibility: PlaylistVisibility | None = Field(description=_VISIBILITY_DESCRIPTION)
    can_manage: bool = Field(description="Текущий пользователь — владелец плейлиста")
    created_at: datetime
    updated_at: datetime


class PlaylistListItem(BaseModel):
    id: uuid.UUID
    title: str
    video_count: int = Field(description=_COUNT_DESCRIPTION)
    thumbnail_url: str | None = Field(description=_THUMB_DESCRIPTION)
    visibility: PlaylistVisibility | None = Field(
        description="В «Моих плейлистах»; на странице канала — null (там только открытые)"
    )
    contains_video: bool | None = Field(
        default=None,
        description="Видео из параметра video_id уже в плейлисте; null — параметр не передан",
    )
    created_at: datetime
    updated_at: datetime


class PlaylistPage(BaseModel):
    items: list[PlaylistListItem] = Field(description="Новые сверху")
    next_cursor: str | None = Field(description="null — это последняя страница")


class PlaylistVideoItem(BaseModel):
    position: int = Field(description="Порядок в плейлисте (возможны пропуски)")
    id: uuid.UUID
    title: str
    channel: ChannelBrief
    duration_seconds: int | None
    views_count: int
    published_at: datetime | None = Field(description="null — ещё не публиковалось")
    thumbnail_url: str | None
    state: VideoStateOut | None = Field(
        description="Только владельцу видео и модераторам/админам; остальным — null"
    )


class PlaylistVideoPage(BaseModel):
    items: list[PlaylistVideoItem] = Field(
        description="Только видео, которые может открыть текущий зритель, по порядку"
    )
    next_cursor: str | None = Field(description="null — это последняя страница")


class PlaylistItemOut(BaseModel):
    playlist_id: uuid.UUID
    video_id: uuid.UUID
    position: int
    added_at: datetime
