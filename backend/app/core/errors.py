"""Доменные ошибки. Сервисы не знают про HTTP — бросают эти исключения,
а обработчики ниже превращают их в единый ответ {"detail": "<строка>"}."""

from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

# Встроенные ответы Starlette — по-русски, тем же форматом.
_HTTP_MESSAGES = {
    404: "Не найдено",
    405: "Метод не поддерживается",
}


class AppError(Exception):
    status_code = 400
    headers: ClassVar[dict[str, str] | None] = None

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


RETRY_AFTER_SECONDS = 2
# Глобальный текст: lock_timeout действует на все блокировки API (строки, advisory-локи смены
# ролей), а не только на видео. Конкретные случаи задают свой текст через Busy(...).
DB_BUSY = "Ресурс сейчас занят. Повторите попытку через несколько секунд"


class Busy(AppError):
    """Временная занятость (чужая блокировка строки): повторить позже."""

    status_code = 503
    headers: ClassVar[dict[str, str] | None] = {"Retry-After": str(RETRY_AFTER_SECONDS)}


# SQLSTATE: lock_timeout истёк (lock_not_available), взаимная блокировка (deadlock_detected)
# и statement_timeout истёк (query_canceled) — временная перегрузка, повторить позже.
_BUSY_SQLSTATES = frozenset({"55P03", "40P01", "57014"})


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig.__cause__, "sqlstate", None)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else exc.headers
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError) -> JSONResponse:
        if _sqlstate(exc) in _BUSY_SQLSTATES:
            return JSONResponse({"detail": DB_BUSY}, status_code=503, headers=Busy.headers)
        raise exc  # прочие ошибки БД — 500, как раньше

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
