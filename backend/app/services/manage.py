"""Управление своими видео: правка метаданных и видимости (дальше — допуск, удаление).

Права — через policies (ADR 0006): управлять может только владелец (`can_manage`).
Ответ на отказ:
  - видео нельзя даже открыть (`can_view` = false) → 404, как при просмотре: существование
    приватного или скрытого видео не раскрывается;
  - открыть можно, а управлять нельзя (чужое публичное, модератор) → 403: факт
    существования зрителю и так известен, а 404 был бы неправдой.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound
from app.models import User
from app.repositories import videos as videos_repo
from app.schemas.videos import VideoCardOut, VideoUpdateIn
from app.services import policies
from app.services import videos as videos_service
from app.services.publication import publish_if_due
from app.services.videos import VIDEO_NOT_FOUND, Viewable

NOT_OWNER = "Управлять видео может только его владелец"


async def get_manageable(db: AsyncSession, viewer: User, video_id: uuid.UUID) -> Viewable:
    """Видео владельца с блокировкой строки до конца транзакции. Отказ — 404 или 403.

    Сначала права (чтение без блокировки), потом блокировка: чужой запрос не должен ставить
    строку видео в очередь блокировок (аудит 3a)."""
    viewable = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    if not policies.can_manage(viewer, viewable.channel.owner_id):
        raise Forbidden(NOT_OWNER)
    video = await videos_repo.lock_for_edit(db, video_id)
    if video is None:  # удалено между проверкой и блокировкой
        raise NotFound(VIDEO_NOT_FOUND)
    return Viewable(video, viewable.channel)


async def update_video(
    db: AsyncSession, viewer: User, video_id: uuid.UUID, data: VideoUpdateIn
) -> VideoCardOut:
    video = (await get_manageable(db, viewer, video_id)).video
    for field in data.model_fields_set:
        setattr(video, field, getattr(data, field))
    # Готовое видео впервые стало не приватным — публикуется (иначе не попадёт в ленту).
    publish_if_due(video)
    await db.commit()
    return await videos_service.card(db, viewer, video_id)
