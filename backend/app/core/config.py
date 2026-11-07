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

    max_upload_bytes: int = 2 * 1024**3  # 2 ГБ
    max_video_seconds: int = 60 * 60  # 60 минут

    health_timeout_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
