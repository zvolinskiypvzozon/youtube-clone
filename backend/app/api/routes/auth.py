from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    DbSession,
    OptionalUser,
    no_store,
    oauth2_scheme,
    session_token,
)
from app.core import security
from app.core.config import get_settings
from app.schemas.auth import LoginIn, MeOut, RegisterIn, TokenOut
from app.schemas.common import err
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(no_store)])


def _client(request: Request) -> auth_service.ClientInfo:
    return auth_service.ClientInfo(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def _previous_cookie_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def _set_auth_cookies(response: Response, token: str) -> None:
    settings = get_settings()
    common = {
        "max_age": settings.session_ttl_days * 24 * 3600,
        "samesite": "lax",
        "secure": settings.cookie_secure,
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    # CSRF-cookie читает JS (не HttpOnly) и повторяет в заголовке X-CSRF-Token.
    # Значение привязано к сессии — сервер проверяет заголовок по HMAC, а не по cookie.
    response.set_cookie(
        CSRF_COOKIE, security.csrf_for(token, settings.secret_key), httponly=False, **common
    )


def _clear_auth_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=MeOut,
    summary="Регистрация (создаёт канал и сразу выполняет вход)",
    responses={409: err("Email или имя уже заняты"), 422: err("Неверный формат полей")},
)
async def register(data: RegisterIn, request: Request, response: Response, db: DbSession) -> MeOut:
    result = await auth_service.register(
        db, data, _client(request), previous_token=_previous_cookie_token(request)
    )
    _set_auth_cookies(response, result.token)
    return MeOut.model_validate(await auth_service.get_me(db, result.user.id))


@router.post(
    "/login",
    response_model=MeOut,
    summary="Вход по email или имени пользователя (cookie-сессия для браузера)",
    responses={401: err("Неверный логин или пароль"), 403: err("Аккаунт заблокирован")},
)
async def login(data: LoginIn, request: Request, response: Response, db: DbSession) -> MeOut:
    result = await auth_service.login(
        db,
        data.login,
        data.password,
        _client(request),
        previous_token=_previous_cookie_token(request),
    )
    _set_auth_cookies(response, result.token)
    return MeOut.model_validate(await auth_service.get_me(db, result.user.id))


@router.post(
    "/token",
    response_model=TokenOut,
    summary="Bearer-токен для Swagger и API-клиентов (OAuth2 password flow)",
    responses={401: err("Неверный логин или пароль"), 403: err("Аккаунт заблокирован")},
)
async def token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], request: Request, db: DbSession
) -> TokenOut:
    result = await auth_service.login(db, form.username, form.password, _client(request))
    return TokenOut(access_token=result.token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Выход: сессия удаляется на сервере, cookie очищаются (идемпотентно)",
    responses={403: err("Неверный CSRF-токен")},
)
async def logout(
    request: Request,
    db: DbSession,
    user: OptionalUser,
    bearer: Annotated[str | None, Depends(oauth2_scheme)],
) -> Response:
    # Действующая cookie-сессия уже прошла CSRF-проверку в зависимости OptionalUser.
    # Недействительная (истекла, отозвана, бан) — просто чистим cookie: выход идемпотентен.
    token_value, _ = session_token(request, bearer)
    if user is not None and token_value:
        await auth_service.logout(db, token_value)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.headers["Cache-Control"] = "no-store"
    _clear_auth_cookies(response)
    return response


@router.get(
    "/me",
    response_model=MeOut,
    summary="Текущий пользователь",
    responses={401: err("Требуется вход")},
)
async def me(user: CurrentUser, db: DbSession) -> MeOut:
    return MeOut.model_validate(await auth_service.get_me(db, user.id))
