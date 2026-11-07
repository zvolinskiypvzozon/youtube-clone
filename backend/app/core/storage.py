"""Доступ к объектному хранилищу (S3 API: silo/MinIO).

Приложение работает с хранилищем только через S3 API, поэтому реализация
хранилища заменяема без изменения кода (см. docs/adr/0002).
"""

import threading
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

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


# --- Операции с объектами (блокирующие: вызывать через asyncio.to_thread) ---------------


@lru_cache(maxsize=32)
def _presign_client(endpoint_url: str) -> BaseClient:
    """Клиент только для подписи URL (сетевых запросов не делает).

    Подпись SigV4 включает host, поэтому подписываем на адрес, по которому к хранилищу
    обратится клиент. Кэш ограничен: endpoint строится из заголовка Host запроса.
    """
    return _client(endpoint_url, read_timeout=5, attempts=1)


def create_multipart_upload(bucket: str, key: str) -> str:
    # Content-Type объекта фиксирует сервер: клиент не может выдать загруженное за HTML.
    response = get_s3().create_multipart_upload(
        Bucket=bucket, Key=key, ContentType="application/octet-stream"
    )
    return response["UploadId"]


def presign_upload_parts(
    endpoint_url: str, bucket: str, key: str, upload_id: str, sizes: list[int], ttl: int
) -> list[str]:
    """Ссылки на части. Content-Length каждой части входит в подпись: хранилище отклонит
    часть любой другой длины (SignatureDoesNotMatch). Сумма частей = заявленный размер —
    загрузить больше заявленного (до complete) физически нельзя."""
    client = _presign_client(endpoint_url)
    return [
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
