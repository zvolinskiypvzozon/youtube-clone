"""Управление своими видео: правка метаданных и видимости, список допуска, удаление.

Права — через policies (ADR 0006): управлять может только владелец (`can_manage`).
Ответ на отказ:
  - видео нельзя даже открыть (`can_view` = false) → 404, как при просмотре: существование
    приватного или скрытого видео не раскрывается;
  - открыть можно, а управлять нельзя (чужое публичное, модератор) → 403: факт
    существования зрителю и так известен, а 404 был бы неправдой.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.models import User
from app.repositories import access as access_repo
from app.repositories import users as users_repo
from app.repositories import videos as videos_repo
from app.schemas.videos import AccessEntryOut, AccessListOut, VideoCardOut, VideoUpdateIn
from app.services import deletion, policies
from app.services import videos as videos_service
from app.services.publication import publish_if_due
from app.services.videos import VIDEO_NOT_FOUND, Viewable

NOT_OWNER = "Управлять видео может только его владелец"
USER_NOT_FOUND = "Пользователь не найден"
SELF_GRANT = "Владелец и так видит своё видео"
ACCESS_LIMIT = 100
ACCESS_LIMIT_REACHED = f"В списке допуска уже {ACCESS_LIMIT} пользователей — это максимум"


async def check_manageable(db: AsyncSession, viewer: User, video_id: uuid.UUID) -> Viewable:
    """Видео владельца без блокировки (чтение, отзыв допуска). Отказ — 404 или 403."""
    viewable = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    if not policies.can_manage(viewer, viewable.channel.owner_id):
        raise Forbidden(NOT_OWNER)
    return viewable


async def get_manageable(db: AsyncSession, viewer: User, video_id: uuid.UUID) -> Viewable:
    """Видео владельца с блокировкой строки до конца транзакции. Отказ — 404 или 403.

    Сначала права (чтение без блокировки), потом блокировка: чужой запрос не должен ставить
    строку видео в очередь блокировок (аудит 3a)."""
    viewable = await check_manageable(db, viewer, video_id)
    video = await videos_repo.lock_for_edit(db, video_id)
    if video is None:  # удалено между проверкой и блокировкой
        raise NotFound(VIDEO_NOT_FOUND)
    return Viewable(video, viewable.channel, viewable.owner_banned)


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


# --- Список допуска -------------------------------------------------------------------------
# Действует только для приватного видео (политика), но ведётся при любой видимости: владелец
# может заранее настроить список и потом сделать видео приватным.


async def list_access(db: AsyncSession, viewer: User, video_id: uuid.UUID) -> AccessListOut:
    await check_manageable(db, viewer, video_id)
    entries = await access_repo.list_entries(db, video_id)
    return AccessListOut(
        items=[
            AccessEntryOut(user_id=e.user_id, username=name, granted_at=e.granted_at)
            for e, name in entries
        ],
        max_entries=ACCESS_LIMIT,
    )


async def grant_access(
    db: AsyncSession, viewer: User, video_id: uuid.UUID, username: str
) -> tuple[AccessEntryOut, bool]:
    """Выдаёт допуск. Возвращает запись и признак «создана» (201) или «уже была» (200).

    Лимит считается под блокировкой строки видео (FOR NO KEY UPDATE): параллельные выдачи
    сериализуются, иначе обе увидели бы 99 записей и добавили по одной."""
    await get_manageable(db, viewer, video_id)
    target = await users_repo.get_by_username(db, username)
    if target is None:
        raise NotFound(USER_NOT_FOUND)
    if target.id == viewer.id:
        raise BadRequest(SELF_GRANT)
    existing = await access_repo.get(db, video_id, target.id)
    if existing is not None:
        entry = AccessEntryOut(
            user_id=target.id, username=target.username, granted_at=existing.granted_at
        )
        # Ничего не меняли — commit только снимает блокировку строки видео. Не rollback:
        # он пометил бы устаревшими объекты сессии (и зрителя), чтение их атрибутов позже
        # в async-коде упало бы.
        await db.commit()
        return entry, False
    if await access_repo.count(db, video_id) >= ACCESS_LIMIT:
        raise Conflict(ACCESS_LIMIT_REACHED)
    granted_at = await access_repo.add(db, video_id, target.id)
    if granted_at is None:  # не должно случиться под блокировкой — защита от двойной вставки
        raise Conflict("Допуск уже выдан")
    entry = AccessEntryOut(user_id=target.id, username=target.username, granted_at=granted_at)
    await db.commit()
    return entry, True


async def revoke_access(
    db: AsyncSession, viewer: User, video_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Отзыв идемпотентен: записи нет — тоже успех. Блокировка не нужна: лимит удаление
    не нарушает."""
    await check_manageable(db, viewer, video_id)
    await access_repo.remove(db, video_id, user_id)
    await db.commit()


# --- Удаление -------------------------------------------------------------------------------


async def delete_video(db: AsyncSession, viewer: User, video_id: uuid.UUID) -> None:
    """Права — до любых блокировок (чужой DELETE не встаёт в очередь на строки)."""
    await check_manageable(db, viewer, video_id)
    await deletion.delete_video(db, video_id)
