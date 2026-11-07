"""Схема OpenAPI, согласованная с реальными ответами.

FastAPI сам добавляет к эндпоинтам с параметрами ответ 422 со схемой HTTPValidationError,
где detail — список. Наш обработчик (core/errors.py) отвечает иначе: detail — строка для
показа пользователю, errors — список проблемных полей. Здесь схема приводится к факту.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "title": "HTTPValidationError",
    "type": "object",
    "required": ["detail", "errors"],
    "properties": {
        "detail": {
            "type": "string",
            "title": "Detail",
            "description": "Сообщение для пользователя",
            "examples": ["Проверьте правильность полей: email, password"],
        },
        "errors": {
            "type": "array",
            "title": "Errors",
            "description": "Проблемные поля (loc — путь к полю, msg — описание, type — код)",
            "items": {"$ref": "#/components/schemas/ValidationError"},
        },
    },
}


def install_openapi(app: FastAPI) -> None:
    def build() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schemas = schema.get("components", {}).get("schemas", {})
        if "HTTPValidationError" in schemas:
            schemas["HTTPValidationError"] = VALIDATION_ERROR_SCHEMA
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                response = operation.get("responses", {}).get("422")
                if response is not None:
                    response["description"] = "Неверный формат полей"
        app.openapi_schema = schema
        return schema

    app.openapi = build  # type: ignore[method-assign]
