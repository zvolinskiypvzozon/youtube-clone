"""Все модели импортируются здесь, чтобы Alembic видел полную метадату."""

from app.models.base import Base
from app.models.enums import (
    JobStatus,
    JobType,
    ModerationState,
    ReactionKind,
    UserRole,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from app.models.job import Job
from app.models.user import Channel, User, UserSession
from app.models.video import Comment, Reaction, Video, VideoAccess, VideoFile

__all__ = [
    "Base",
    "Channel",
    "Comment",
    "Job",
    "JobStatus",
    "JobType",
    "ModerationState",
    "Reaction",
    "ReactionKind",
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
