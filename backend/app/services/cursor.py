"""Непрозрачный курсор keyset-пагинации по (момент времени, id): published_at в каталоге,
created_at в «Моих видео».

Клиент не должен строить курсор сам: формат — деталь реализации. Любой битый курсор → 400.
"""

import base64
import binascii
import uuid
from datetime import UTC, datetime

from app.core.errors import BadRequest

BAD_CURSOR = "Некорректный курсор страницы"


def encode(published_at: datetime, video_id: uuid.UUID) -> str:
    raw = f"{published_at.isoformat()}|{video_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, vid = raw.split("|")
        published_at = datetime.fromisoformat(ts)
        if published_at.tzinfo is None:
            raise ValueError("без часового пояса")
        # Нормализация здесь, а не в драйвере: даты на краю диапазона (0001-01-01+05:00)
        # переполняются при переводе в UTC — это 400, а не 500 из asyncpg.
        return published_at.astimezone(UTC), uuid.UUID(vid)
    except (ValueError, OverflowError, binascii.Error, UnicodeDecodeError) as exc:
        raise BadRequest(BAD_CURSOR) from exc
