"""Все модели импортируются здесь, чтобы Alembic видел полную метадату."""

from app.models.base import Base
from app.models.enums import (
    JobStatus,
    JobType,
    ModerationAction,
    ModerationState,
    PlaylistVisibility,
    ReactionKind,
    ReportCategory,
    ReportStatus,
    UserRole,
    VideoFileKind,
    VideoStatus,
    VideoVisibility,
)
from app.models.job import Job
from app.models.moderation import ModerationLogEntry, Report
from app.models.playlist import Playlist, PlaylistItem
from app.models.user import Channel, RetiredUsername, Subscription, User, UserSession
from app.models.video import Comment, Reaction, Video, VideoAccess, VideoFile

__all__ = [
    "Base",
    "Channel",
    "Comment",
    "Job",
    "JobStatus",
    "JobType",
    "ModerationAction",
    "ModerationLogEntry",
    "ModerationState",
    "Playlist",
    "PlaylistItem",
    "PlaylistVisibility",
    "Reaction",
    "ReactionKind",
    "Report",
    "ReportCategory",
    "ReportStatus",
    "RetiredUsername",
    "Subscription",
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
