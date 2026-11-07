"""Доступ к объектному хранилищу (S3 API: silo/MinIO).

Приложение работает с хранилищем только через S3 API, поэтому реализация
хранилища заменяема без изменения кода (см. docs/adr/0002).
"""

import threading
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from app.core.config import get_settings

# Сессия boto3 по умолчанию не потокобезопасна при создании клиентов,
# а первый вызов может прийти одновременно из нескольких потоков (asyncio.to_thread).
_lock = threading.Lock()


def _client(endpoint_url: str, *, read_timeout: float, attempts: int) -> BaseClient:
    settings = get_settings()
    with _lock:
        return boto3.session.Session().client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=settings.health_timeout_seconds,
                read_timeout=read_timeout,
                retries={"max_attempts": attempts, "mode": "standard"},
            ),
        )


@lru_cache
def get_s3() -> BaseClient:
    """Клиент для рабочих операций внутри docker-сети."""
    return _client(get_settings().s3_endpoint, read_timeout=30, attempts=2)


@lru_cache
def _get_health_s3() -> BaseClient:
    """Отдельный клиент для health: короткие таймауты, без повторов.

    Зависшее хранилище не должно занимать пул потоков на десятки секунд.
    """
    settings = get_settings()
    return _client(settings.s3_endpoint, read_timeout=settings.health_timeout_seconds, attempts=1)


def check_buckets() -> None:
    """Бросает исключение, если хранилище или бакеты недоступны."""
    settings = get_settings()
    s3 = _get_health_s3()
    for bucket in (settings.s3_bucket_raw, settings.s3_bucket_media):
        s3.head_bucket(Bucket=bucket)
