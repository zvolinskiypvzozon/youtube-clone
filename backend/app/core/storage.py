"""Доступ к объектному хранилищу (S3 API: silo/MinIO).

Приложение работает с хранилищем только через S3 API, поэтому реализация
хранилища заменяема без изменения кода (см. docs/adr/0002).
"""

import threading
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

# Сессия boto3 по умолчанию не потокобезопасна при создании клиентов,
# а первый вызов может прийти одновременно из нескольких потоков (asyncio.to_thread).
_lock = threading.Lock()

# Host, на который подписываются ссылки при относительных URL (ADR 0004, дополнение 1).
# Константа, а не настройка: обязан совпадать с `proxy_set_header Host` в
# infra/nginx/storage-proxy.conf, и расходиться им незачем.
SIGNING_HOST = "media.internal"


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


# --- Операции с объектами (блокирующие: вызывать через asyncio.to_thread) ---------------


def _signing_endpoint() -> str:
    """Адрес, на который подписываются ссылки для клиента (ADR 0004, дополнение 1).

    По умолчанию — постоянный внутренний host (SIGNING_HOST): nginx в /yt-raw/ и /yt-media/
    передаёт хранилищу именно его, а клиент получает относительный URL и ходит на тот же
    origin, что и SPA. Host запроса в подпись не попадает — один клиент на процесс.
    S3_PUBLIC_ENDPOINT (автотесты: клиент ходит в хранилище напрямую) — абсолютные ссылки.
    """
    settings = get_settings()
    if settings.s3_public_endpoint:
        return settings.s3_public_endpoint.rstrip("/")
    return f"http://{SIGNING_HOST}"


@lru_cache(maxsize=2)
def _presign_client(endpoint_url: str) -> BaseClient:
    """Клиент только для подписи URL (сетевых запросов не делает). Ключ кэша — из конфигурации,
    не из запроса: создание клиента стоит ~0,35 с CPU."""
    return _client(endpoint_url, read_timeout=5, attempts=1)


def _for_client(url: str) -> str:
    if get_settings().s3_public_endpoint:
        return url
    parts = urlsplit(url)
    return urlunsplit(("", "", parts.path, parts.query, ""))


def create_multipart_upload(bucket: str, key: str) -> str:
    # Content-Type объекта фиксирует сервер: клиент не может выдать загруженное за HTML.
    response = get_s3().create_multipart_upload(
        Bucket=bucket, Key=key, ContentType="application/octet-stream"
    )
    return response["UploadId"]


def presign_upload_parts(
    bucket: str, key: str, upload_id: str, sizes: list[int], ttl: int
) -> list[str]:
    """Ссылки на части. Content-Length каждой части входит в подпись: хранилище отклонит
    часть любой другой длины (SignatureDoesNotMatch). Сумма частей = заявленный размер —
    загрузить больше заявленного (до complete) физически нельзя."""
    client = _presign_client(_signing_endpoint())
    return [
        _for_client(
            client.generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": bucket,
                    "Key": key,
                    "UploadId": upload_id,
                    "PartNumber": n,
                    "ContentLength": size,
                },
                ExpiresIn=ttl,
            )
        )
        for n, size in enumerate(sizes, start=1)
    ]


def complete_multipart_upload(
    bucket: str, key: str, upload_id: str, parts: list[tuple[int, str]]
) -> None:
    get_s3().complete_multipart_upload(
        Bucket=bucket,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": [{"PartNumber": n, "ETag": etag} for n, etag in parts]},
    )


def abort_multipart_upload(bucket: str, key: str, upload_id: str) -> None:
    try:
        get_s3().abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchUpload":
            raise


def object_size(bucket: str, key: str) -> int:
    return int(get_s3().head_object(Bucket=bucket, Key=key)["ContentLength"])


def object_size_or_none(bucket: str, key: str) -> int | None:
    try:
        return object_size(bucket, key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def delete_object(bucket: str, key: str) -> None:
    get_s3().delete_object(Bucket=bucket, Key=key)


def download_file(bucket: str, key: str, path: str) -> None:
    get_s3().download_file(bucket, key, path)


def upload_file(path: str, bucket: str, key: str, content_type: str) -> None:
    get_s3().upload_file(path, bucket, key, ExtraArgs={"ContentType": content_type})


def delete_objects(bucket: str, keys: list[str]) -> None:
    for key in keys:
        delete_object(bucket, key)


def list_objects(bucket: str, prefix: str) -> list[tuple[str, datetime]]:
    """(ключ, время изменения) всех объектов с префиксом."""
    paginator = get_s3().get_paginator("list_objects_v2")
    result: list[tuple[str, datetime]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        result.extend((o["Key"], o["LastModified"]) for o in page.get("Contents", []))
    return result
