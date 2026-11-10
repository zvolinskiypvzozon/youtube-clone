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


def no_nul(value: str) -> str:
    """NUL в text Postgres не принимает — без этой проверки был бы 500 (как в аудите 3b)."""
    if "\x00" in value:
        raise ValueError("Текст не может содержать NUL-символ")
    return value
