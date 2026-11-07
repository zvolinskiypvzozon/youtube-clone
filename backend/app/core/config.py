from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация из переменных окружения. Секреты в коде не хранятся."""

    model_config = SettingsConfigDict(extra="ignore")

    env: Literal["prod", "dev", "test"] = "prod"
    log_level: str = "INFO"

    database_url: str = Field(description="postgresql+asyncpg://user:pass@host:5432/db")
    secret_key: str = Field(min_length=32)

    s3_endpoint: str = "http://storage:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str
    s3_secret_key: str
    s3_bucket_raw: str = "yt-raw"
    s3_bucket_media: str = "yt-media"

    # Адрес хранилища для presigned URL, которые получает клиент. Пусто — берётся из запроса
    # (схема + Host): браузер ходит в хранилище через nginx на том же origin (ADR 0004).
    # Задаётся, когда клиент обращается к хранилищу напрямую (автотесты: http://storage:9000).
    s3_public_endpoint: str | None = None

    max_upload_bytes: int = 2 * 1024**3  # 2 ГБ
    max_video_seconds: int = 60 * 60  # 60 минут
    upload_part_bytes: int = 16 * 1024**2  # 16 МиБ: 2 ГБ = 128 частей, < лимита nginx 128 МиБ
    upload_url_ttl_seconds: int = 3600
    max_active_uploads: int = 3  # незавершённых загрузок на пользователя
    # Ссылки на просмотр (поток, превью). Подпись проверяется на каждом Range-запросе:
    # после истечения плеер заново запрашивает /stream (день 5). Связано с max-age ответов
    # /yt-media/ в infra/nginx/default.conf.
    stream_url_ttl_seconds: int = 15 * 60

    # Worker (ADR 0003)
    worker_poll_seconds: float = 2.0
    worker_lease_seconds: int = 15 * 60
    worker_heartbeat_seconds: int = 60
    worker_retry_base_seconds: int = 30  # пауза перед повтором: base * 2^(попытка-1)
    worker_maintenance_seconds: int = 300  # периодические задачи
    stale_upload_hours: int = 24  # незавершённые загрузки старше — удаляются
    # Объекты yt-media без ссылок из БД старше порога удаляются. Порог > lease (15 мин):
    # объекты обрабатываемой задачи получают ссылки только при финализации.
    orphan_media_minutes: int = 120
    ffmpeg_threads: int = 2

    health_timeout_seconds: float = 2.0

    # Сессии (ADR 0005)
    session_ttl_days: int = 30
    # Secure-флаг cookie: включать при HTTPS. По ТЗ проверка идёт по HTTP на IP,
    # поэтому по умолчанию выключен (COOKIE_SECURE=true в .env для HTTPS).
    cookie_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
