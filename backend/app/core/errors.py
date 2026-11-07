"""Доменные ошибки. Сервисы не знают про HTTP — бросают эти исключения,
а обработчики ниже превращают их в единый ответ {"detail": "<строка>"}."""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# Встроенные ответы Starlette — по-русски, тем же форматом.
_HTTP_MESSAGES = {
    404: "Не найдено",
    405: "Метод не поддерживается",
}


class AppError(Exception):
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BadRequest(AppError):
    status_code = 400


class Unauthorized(AppError):
    status_code = 401


class Forbidden(AppError):
    status_code = 403


class NotFound(AppError):
    status_code = 404


class Conflict(AppError):
    status_code = 409


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors(), exclude={"input", "url", "ctx"})
        fields = sorted({str(e["loc"][-1]) for e in errors if e.get("loc")})
        detail = (
            "Проверьте правильность полей: " + ", ".join(fields) if fields else "Неверный запрос"
        )
        return JSONResponse({"detail": detail, "errors": errors}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = _HTTP_MESSAGES.get(exc.status_code, exc.detail)
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)
