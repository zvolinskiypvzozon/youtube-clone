"""Лайки и дизлайки (K6).

Одна строка `reactions` на пару (видео, пользователь) — лайк и дизлайк взаимоисключающие;
повтор той же реакции ничего не меняет (идемпотентно). Доступ — `can_view` (ADR 0006:
реакции доступны только тем, кто может видеть видео), реагировать можно только на готовое.

Счётчики сходятся: все изменения реакций одного видео сериализуются блокировкой строки
видео (`FOR NO KEY UPDATE`, после проверки прав), прежняя реакция читается уже под ней,
а счётчики меняются на разницу «было → стало» в той же транзакции. Права и готовность под
блокировкой проверяются заново. Повтор той же реакции блокировку не берёт.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models import ReactionKind, User, Video, VideoStatus
from app.repositories import access as access_repo
from app.repositories import reactions as reactions_repo
from app.repositories import videos as videos_repo
from app.schemas.videos import ReactionOut
from app.services import policies
from app.services import videos as videos_service
from app.services.videos import NOT_READY, VIDEO_NOT_FOUND, Viewable


def _count(kind: ReactionKind | None, of: ReactionKind) -> int:
    return 1 if kind == of else 0


async def set_reaction(
    db: AsyncSession, viewer: User, video_id: uuid.UUID, kind: ReactionKind | None
) -> ReactionOut:
    """kind=None — снять реакцию."""
    viewable = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    if viewable.video.status != VideoStatus.ready:
        raise Conflict(NOT_READY)
    # Повтор той же реакции — ответ без блокировки строки видео (самый дешёвый спам-вектор
    # не должен занимать строку, нужную правке, worker-у и удалению; аудит).
    if await reactions_repo.get_kind(db, video_id, viewer.id) == kind:
        video = viewable.video
        out = ReactionOut(
            my_reaction=kind, likes_count=video.likes_count, dislikes_count=video.dislikes_count
        )
        await db.commit()
        return out

    video = await _lock_viewable(db, viewer, viewable)
    old = await reactions_repo.get_kind(db, video_id, viewer.id)  # под блокировкой
    if old == kind:  # параллельный запрос того же пользователя успел раньше
        out = ReactionOut(
            my_reaction=kind, likes_count=video.likes_count, dislikes_count=video.dislikes_count
        )
        await db.commit()
        return out

    if kind is None:
        await reactions_repo.remove(db, video_id, viewer.id)
    else:
        await reactions_repo.put(db, video_id, viewer.id, kind)
    likes, dislikes = await videos_repo.add_reaction_counts(
        db,
        video_id,
        _count(kind, ReactionKind.like) - _count(old, ReactionKind.like),
        _count(kind, ReactionKind.dislike) - _count(old, ReactionKind.dislike),
    )
    await db.commit()
    return ReactionOut(my_reaction=kind, likes_count=likes, dislikes_count=dislikes)


async def _lock_viewable(db: AsyncSession, viewer: User, viewable: Viewable) -> Video:
    """Блокирует строку видео и заново проверяет права уже по ней: пока запрос ждал
    блокировку, владелец мог сделать видео приватным (PATCH берёт ту же блокировку), отозвать
    допуск или удалить видео. Без перепроверки реакция записалась бы на видео, которое
    зрителю уже недоступно (ревью и аудит шага 1)."""
    video = await videos_repo.lock_for_edit(db, viewable.video.id)
    if video is None:  # удалено, пока ждали
        await db.rollback()
        raise NotFound(VIDEO_NOT_FOUND)
    has_access = await access_repo.get(db, video.id, viewer.id) is not None
    facts = policies.VideoFacts.of(
        video, owner_id=viewable.channel.owner_id, viewer_has_access=has_access
    )
    if not policies.can_view(viewer, facts):
        await db.rollback()
        raise NotFound(VIDEO_NOT_FOUND)
    if video.status != VideoStatus.ready:
        await db.rollback()
        raise Conflict(NOT_READY)
    return video
