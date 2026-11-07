"""Все модели импортируются здесь, чтобы Alembic видел полную метадату."""

from app.models.base import Base
from app.models.enums import (
    JobStatus,
    JobType,
    ModerationState,
    UserRole,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from app.models.job import Job
from app.models.user import Channel, User, UserSession
from app.models.video import Video, VideoAccess, VideoFile

__all__ = [
    "Base",
    "Channel",
    "Job",
    "JobStatus",
    "JobType",
    "ModerationState",
    "User",
    "UserRole",
    "UserSession",
    "Video",
    "VideoAccess",
    "VideoFile",
    "VideoFileKind",
    "VideoStatus",
    "VideoVisibility",
]
