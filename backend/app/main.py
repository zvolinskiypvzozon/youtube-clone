from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title="YouTube Clone API",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.include_router(api_router)
    return app


app = create_app()
