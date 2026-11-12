from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import setup_logging
from app.core.openapi import install_openapi

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


async def reject_cross_origin(request: Request, call_next):
    """CSRF, первый слой: изменяющий запрос из браузера с чужого сайта отклоняется.

    Браузер всегда шлёт Origin на POST/PUT/PATCH/DELETE. Нет Origin — не браузер
    (curl, API-клиент): для них CSRF неприменим. Покрывает и login CSRF на /auth/login,
    где сессии (и double submit) ещё нет.
    """
    origin = request.headers.get("origin")
    cross_site = False
    if origin is not None:
        parts = urlsplit(origin)
        # Схема — из X-Forwarded-Proto (nginx), порт — внутри netloc и Host.
        cross_site = (parts.scheme, parts.netloc) != (
            request.url.scheme,
            request.headers.get("host"),
        )
    if request.method in UNSAFE_METHODS and cross_site:
        return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, status_code=403)
    return await call_next(request)


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title="YouTube Clone API",
        version="0.1.0",
        # Swagger UI отдаёт nginx своими файлами (без CDN): infra/nginx/swagger/.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    install_error_handlers(app)
    app.middleware("http")(reject_cross_origin)
    app.include_router(api_router)
    install_openapi(app)
    return app


app = create_app()
