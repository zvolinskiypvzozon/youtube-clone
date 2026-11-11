import uuid
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, StringConstraints

from app.models import VideoStatus, VideoVisibility
from app.schemas.text import blank_if_invisible, clean_text, require_visible, single_line

MAX_UPLOAD_BYTES = 2 * 1024**3

# Порядок метаданных — app/schemas/text.py: чистка → обрезка и длина → проверка видимости.
# BeforeValidator: последний в списке выполняется первым (clean_text, затем single_line).
# Длина — после обрезки пробелов по краям, как в CHECK title_length.
Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    BeforeValidator(single_line),
    BeforeValidator(clean_text),
    AfterValidator(require_visible),
]
# Описание без видимых символов — пустое (необязательное поле).
Description = Annotated[
    str,
    StringConstraints(max_length=5000),
    BeforeValidator(clean_text),
    AfterValidator(blank_if_invisible),
]


class UploadCreateIn(BaseModel):
    title: Title = Field(examples=["Мой первый ролик"])
    description: Description = ""
    # Обязательное поле: в форме по умолчанию «Открытый доступ», но API не угадывает.
    visibility: VideoVisibility
    content_type: str = Field(
        pattern=r"^video/[a-z0-9.+-]{1,50}$",
        description="MIME-тип файла (video/*) — ранняя подсказка; формат проверяет ffprobe",
        examples=["video/mp4"],
    )
    # Жёсткий потолок схемы; рабочий лимит — settings.max_upload_bytes (проверяет сервис).
    size_bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES, description="Размер файла, ≤ 2 ГБ")


class UploadPartUrl(BaseModel):
    number: int
    size: int = Field(description="Точная длина части в байтах — входит в подпись ссылки")
    url: str


class UploadCreateOut(BaseModel):
    video_id: uuid.UUID
    part_size: int = Field(description="Размер каждой части, кроме последней")
    expires_in: int = Field(description="Срок действия ссылок, секунд")
    parts: list[UploadPartUrl]


class CompletedPart(BaseModel):
    number: int = Field(ge=1, le=10_000)
    etag: str = Field(
        min_length=1, max_length=128, description="Заголовок ETag ответа на PUT части"
    )


class UploadCompleteIn(BaseModel):
    parts: list[CompletedPart] = Field(min_length=1, max_length=10_000)


class UploadStatusOut(BaseModel):
    video_id: uuid.UUID
    status: VideoStatus
