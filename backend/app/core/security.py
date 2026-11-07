"""Пароли (argon2id) и токены сессий.

Токен сессии — 256 бит случайных данных; клиент хранит его в HttpOnly-cookie,
в БД лежит только SHA-256 (утечка БД не даёт войти чужими сессиями).
"""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Параметры argon2-cffi по умолчанию — профиль RFC 9106 (argon2id, 64 МиБ, t=3, p=4).
_hasher = PasswordHasher()

# Для несуществующего пользователя тоже выполняем verify — время ответа не выдаёт,
# зарегистрирован ли логин.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def burn_password_check(password: str) -> None:
    verify_password(_DUMMY_HASH, password)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def csrf_for(session_token: str, secret_key: str) -> str:
    """CSRF-токен, привязанный к сессии: HMAC(secret_key, токен сессии).

    Подложенная cookie `csrf` (например, сервисом на другом порту того же IP — cookie
    не изолированы по портам) не поможет: сервер сверяет заголовок с вычисленным значением.
    """
    return hmac.new(secret_key.encode(), session_token.encode(), hashlib.sha256).hexdigest()
