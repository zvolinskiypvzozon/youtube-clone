from enum import StrEnum

from sqlalchemy import Enum as SAEnum


class UserRole(StrEnum):
    user = "user"
    moderator = "moderator"
    admin = "admin"


class VideoVisibility(StrEnum):
    public = "public"  # лента, поиск, канал
    unlisted = "unlisted"  # только по ссылке
    private = "private"  # владелец + список допуска + модераторы/админы


class VideoStatus(StrEnum):
    uploading = "uploading"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class ModerationState(StrEnum):
    ok = "ok"
    hidden = "hidden"


class VideoFileKind(StrEnum):
    original = "original"
    mp4_360 = "mp4_360"
    mp4_720 = "mp4_720"
    thumbnail = "thumbnail"


class JobType(StrEnum):
    process_video = "process_video"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


def pg_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Postgres ENUM, в БД хранятся значения (а не имена) членов перечисления."""
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
