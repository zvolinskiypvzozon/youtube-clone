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

from app.models import (
    ModerationState,
    ReportCategory,
    ReportStatus,
    UserRole,
    VideoStatus,
    VideoVisibility,
)
from app.schemas.common import no_nul


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    username: str
    role: UserRole
    is_banned: bool
    created_at: datetime


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int


class RoleUpdateIn(BaseModel):
    role: UserRole


# --- Модерация ------------------------------------------------------------------------------

MODERATION_REASON_MAX = 500

# Причина действия модератора: пробелы по краям обрезаются, 1–500 символов, без NUL.
ModerationReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MODERATION_REASON_MAX),
    AfterValidator(no_nul),
]


class ReporterOut(BaseModel):
    id: uuid.UUID
    username: str


class ReportVideoOut(BaseModel):
    """Снимок на момент жалобы и текущее состояние видео (None — видео удалено)."""

    id: uuid.UUID | None
    title: str = Field(description="Название на момент жалобы")
    channel_handle: str = Field(description="Канал на момент жалобы")
    moderation: ModerationState | None
    visibility: VideoVisibility | None
    status: VideoStatus | None


class ReportQueueItem(BaseModel):
    id: uuid.UUID
    category: ReportCategory
    comment: str | None = Field(description="Обычный текст: выводить только как текст")
    status: ReportStatus
    created_at: datetime
    reporter: ReporterOut | None = Field(description="null — пользователь удалён")
    video: ReportVideoOut
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None


class ReportQueuePage(BaseModel):
    items: list[ReportQueueItem] = Field(description="Новые сверху")
    next_cursor: str | None = Field(description="null — это последняя страница")


class VideoModerationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ModerationState = Field(description="hidden — скрыть, ok — вернуть")
    reason: ModerationReason | None = Field(
        default=None, description="Причина: обязательна при скрытии, её видит владелец видео"
    )

    @model_validator(mode="after")
    def _reason_for_hide(self) -> "VideoModerationIn":
        if self.state == ModerationState.hidden and self.reason is None:
            raise ValueError("При скрытии видео нужно указать причину")
        return self


class VideoModerationOut(BaseModel):
    video_id: uuid.UUID
    moderation: ModerationState
    moderation_reason: str | None
    closed_reports: int = Field(description="Сколько открытых жалоб закрыто этим действием")


class ReportDismissIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ModerationReason | None = None


class ReportDismissOut(BaseModel):
    id: uuid.UUID
    status: ReportStatus
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
