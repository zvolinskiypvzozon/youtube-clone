"""Общие зависимости FastAPI: текущий пользователь и CSRF.

Аутентификация двумя способами (один и тот же токен сессии):
  - cookie `session` (HttpOnly) — браузер/SPA;
  - `Authorization: Bearer <token>` — Swagger и API-клиенты.

CSRF (для cookie): изменяющий запрос должен нести заголовок X-CSRF-Token, равный
HMAC(secret_key, токен сессии). То же значение сервер кладёт в читаемую JS cookie `csrf`.
Bearer браузер сам не подставляет — для него проверка не нужна.
Дополнительно main.py отклоняет изменяющие запросы с чужим Origin.

Транзакции: сервисы завершают работу явным `await db.commit()` (или rollback) и не используют
`async with db.begin()`. Зависимость аутентификации всегда закрывает свою транзакцию,
поэтому сервис получает сессию БД в чистом состоянии.
"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.db import get_session
from app.core.errors import Forbidden, Unauthorized
from app.models import User
from app.services import auth as auth_service

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DbSession = Annotated[AsyncSession, Depends(get_session)]

# tokenUrl — для кнопки Authorize в Swagger.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


def session_token(request: Request, bearer: str | None) -> tuple[str | None, bool]:
    """(токен, пришёл ли он из cookie)."""
    if bearer:
        return bearer, False
    cookie = request.cookies.get(SESSION_COOKIE)
    return (cookie, True) if cookie else (None, False)


def check_csrf(request: Request, session_token_value: str) -> None:
    header = request.headers.get(CSRF_HEADER)
    expected = security.csrf_for(session_token_value, get_settings().secret_key)
    if not header or not security.tokens_equal(header, expected):
        raise Forbidden("CSRF-токен отсутствует или неверен")


async def get_optional_user(
    request: Request,
    db: DbSession,
    bearer: Annotated[str | None, Depends(oauth2_scheme)],
) -> User | None:
    token, from_cookie = session_token(request, bearer)
    if token is None:
        return None
    user = await auth_service.authenticate(db, token)
    if user is not None and from_cookie and request.method not in SAFE_METHODS:
        check_csrf(request, token)
    return user


async def get_current_user(
    user: Annotated[User | None, Depends(get_optional_user)],
) -> User:
    if user is None:
        raise Unauthorized("Требуется вход")
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]
CurrentUser = Annotated[User, Depends(get_current_user)]
