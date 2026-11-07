from typing import Any

from pydantic import BaseModel


class ErrorOut(BaseModel):
    """Единый формат ошибки: detail — строка для показа пользователю.
    Для 422 дополнительно errors — список проблемных полей (формат Pydantic)."""

    detail: str
    errors: list[dict[str, Any]] | None = None


def err(description: str) -> dict:
    """Описание ответа-ошибки для Swagger: responses={401: err("...")}."""
    return {"model": ErrorOut, "description": description}
