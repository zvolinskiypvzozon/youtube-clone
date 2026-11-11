import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.models import ReportCategory, ReportStatus
from app.schemas.text import clean_text, none_if_invisible

REPORT_COMMENT_MAX = 500

# Необязательный текст жалобы: пробелы по краям обрезаются; текст без видимых символов
# (в том числе пустой) — как отсутствие пояснения.
ReportComment = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=REPORT_COMMENT_MAX),
    BeforeValidator(clean_text),
    AfterValidator(none_if_invisible),
]


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ReportCategory
    comment: ReportComment | None = Field(
        default=None, description="Необязательное пояснение, до 500 символов"
    )


class ReportCreatedOut(BaseModel):
    """Подтверждение жалобы. Жалобы целиком видит только staff (очередь модерации)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: ReportCategory
    status: ReportStatus
    created_at: datetime
