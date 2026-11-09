"""Обработчик ошибок БД: занятость (lock_timeout, взаимная блокировка) → 503 + Retry-After,
прочие ошибки БД — по-прежнему 500, а не «занято, повторите»."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError

from app.core.errors import DB_BUSY, install_error_handlers


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Wrapped(Exception):
    """Как адаптер asyncpg в SQLAlchemy: sqlstate у исходной ошибки драйвера (__cause__)."""


def _app(error: Exception) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise error

    return app


def _db_error(sqlstate: str, *, nested: bool = False) -> DBAPIError:
    orig: Exception = _DriverError(sqlstate)
    if nested:
        wrapped = _Wrapped("adapted")
        wrapped.__cause__ = orig
        orig = wrapped
    return DBAPIError("SELECT 1", None, orig)


async def _get(error: Exception):
    transport = ASGITransport(app=_app(error), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/boom")


@pytest.mark.parametrize("sqlstate", ["55P03", "40P01"])
@pytest.mark.parametrize("nested", [False, True])
async def test_busy_errors_are_503(sqlstate: str, nested: bool) -> None:
    r = await _get(_db_error(sqlstate, nested=nested))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"
    assert r.json() == {"detail": DB_BUSY}


@pytest.mark.parametrize("sqlstate", ["23505", "23502", "57014", "08006"])
async def test_other_db_errors_stay_500(sqlstate: str) -> None:
    r = await _get(_db_error(sqlstate))
    assert r.status_code == 500
    assert sqlstate not in r.text and "SELECT" not in r.text  # детали наружу не уходят
