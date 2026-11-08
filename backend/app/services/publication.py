"""Момент публикации видео — одно правило для worker (финализация) и правки владельцем.

`published_at` — первая публикация: видео готово и не приватно. Дальше не меняется
(смена видимости туда-обратно не поднимает видео в ленте). Без него готовое видео
никогда не попадёт в каталог: лента и канал сортируются по `published_at`.
"""

from datetime import UTC, datetime

from app.models import Video, VideoStatus, VideoVisibility


def publish_if_due(video: Video) -> None:
    if (
        video.published_at is None
        and video.status == VideoStatus.ready
        and video.visibility != VideoVisibility.private
    ):
        video.published_at = datetime.now(UTC)
