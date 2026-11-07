import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.storage import check_buckets

router = APIRouter(prefix="/health", tags=["health"])
log = logging.getLogger(__name__)

CheckState = Literal["ok", "fail"]


class LiveOut(BaseModel):
    status: Literal["ok"] = "ok"


class HealthOut(BaseModel):
    status: CheckState
    database: CheckState
    storage: CheckState


async def _check_database() -> CheckState:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        log.exception("health: database check failed")
        return "fail"


async def _check_storage() -> CheckState:
    try:
        await asyncio.to_thread(check_buckets)
        return "ok"
    except Exception:
        log.exception("health: storage check failed")
        return "fail"


async def _with_timeout(coro, seconds: float) -> CheckState:
    try:
        return await asyncio.wait_for(coro, seconds)
    except TimeoutError:
        return "fail"


@router.get("/live", response_model=LiveOut, summary="Процесс жив (liveness)")
async def live() -> LiveOut:
    return LiveOut()


@router.get(
    "",
    response_model=HealthOut,
    summary="Готовность: БД и хранилище доступны (readiness)",
    responses={503: {"model": HealthOut, "description": "Зависимость недоступна"}},
)
async def health(response: Response) -> HealthOut:
    timeout = get_settings().health_timeout_seconds * 2
    database, storage = await asyncio.gather(
        _with_timeout(_check_database(), timeout),
        _with_timeout(_check_storage(), timeout),
    )
    overall: CheckState = "ok" if database == storage == "ok" else "fail"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(status=overall, database=database, storage=storage)
