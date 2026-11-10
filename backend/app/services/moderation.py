"""Модерация (staff: moderator и admin): очередь жалоб, скрытие и возврат видео, отклонение
жалобы. Удаление комментария staff — в services/comments.py (та же запись в журнал).

Права — роль staff (зависимость роута). Staff видит любое видео (`policies.can_view`),
поэтому несуществующее видео или жалоба — 404 без вопроса о видимости.

Порядок блокировок «видео → жалобы» (ADR 0006 доп. 6):
  - скрытие: строка видео FOR NO KEY UPDATE (lock_for_edit), затем UPDATE открытых жалоб.
    Жалоба держит строку видео FOR SHARE до commit, поэтому скрытие ждёт её и закрывает
    и её тоже; жалоба, пришедшая во время скрытия, ждёт его и получает 404/409;
  - отклонение: жалоба читается без блокировки (ради video_id), затем строка видео, затем
    UPDATE жалобы. Обратный порядок (жалоба, потом видео) взаимно блокировался бы
    с удалением видео: оно держит видео FOR UPDATE и каскадом (SET NULL) ждёт строки жалоб;
  - удаление видео: задачи → видео → жалобы (SET NULL каскадом), services/deletion.

Каждое действие пишется в moderation_log со снимком цели. Повтор того же состояния (скрыть
скрытое с той же причиной, вернуть видимое) — без изменений и без записи в журнал.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound
from app.models import ModerationAction, ModerationState, ReportStatus, User
from app.repositories import moderation as moderation_repo
from app.repositories import videos as videos_repo
from app.schemas.admin import (
    ReportDismissOut,
    ReporterOut,
    ReportQueueItem,
    ReportQueuePage,
    ReportVideoOut,
    VideoModerationOut,
)
from app.services import cursor, policies
from app.services.videos import VIDEO_NOT_FOUND

REPORT_NOT_FOUND = "Жалоба не найдена"
REPORT_CLOSED = "Жалоба уже закрыта"
OWN_VIDEO = "Модерировать своё видео может только администратор"


async def list_reports(
    db: AsyncSession, viewer: User, status: ReportStatus, after_cursor: str | None, limit: int
) -> ReportQueuePage:
    """Очередь: модератору — без жалоб на его видео (policies.moderation_queue_filter), иначе
    он узнал бы, кто на него пожаловался; админу — все."""
    after = cursor.decode(after_cursor) if after_cursor else None
    visible = policies.moderation_queue_filter(viewer)
    rows = await moderation_repo.queue_page(db, visible, status, after, limit + 1)
    await db.commit()  # только чтение
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        ReportQueueItem(
            id=r.id,
            category=r.category,
            comment=r.comment,
            status=r.status,
            created_at=r.created_at,
            reporter=(
                ReporterOut(id=r.reporter_id, username=name)
                if r.reporter_id is not None and name is not None
                else None
            ),
            video=ReportVideoOut(
                id=r.video_id,
                title=r.video_title,
                channel_handle=r.channel_handle,
                moderation=v.moderation if v else None,
                visibility=v.visibility if v else None,
                status=v.status if v else None,
            ),
            resolved_by=r.resolved_by,
            resolved_at=r.resolved_at,
        )
        for r, name, v in rows
    ]
    last = rows[-1][0] if rows else None
    next_cursor = cursor.encode(last.created_at, last.id) if has_more and last else None
    return ReportQueuePage(items=items, next_cursor=next_cursor)


async def set_video_moderation(
    db: AsyncSession,
    actor: User,
    video_id: uuid.UUID,
    state: ModerationState,
    reason: str | None,
) -> VideoModerationOut:
    """Скрыть (причину видит владелец; открытые жалобы закрываются как resolved) или вернуть
    видео (причина снимается; закрытые жалобы не открываются — история в журнале)."""
    actor_id = actor.id  # до любых rollback: они просрочивают объекты сессии
    # Права — до блокировки: отказ не встаёт в очередь на строку видео.
    owner_id = await moderation_repo.video_owner(db, video_id)
    if owner_id is None:
        raise NotFound(VIDEO_NOT_FOUND)
    if not policies.can_moderate(actor, owner_id):
        raise Forbidden(OWN_VIDEO)
    video = await videos_repo.lock_for_edit(db, video_id)  # видео → жалобы
    if video is None:
        await db.rollback()
        raise NotFound(VIDEO_NOT_FOUND)
    new_reason = reason if state == ModerationState.hidden else None
    if video.moderation == state and video.moderation_reason == new_reason:
        out = VideoModerationOut(
            video_id=video.id, moderation=state, moderation_reason=new_reason, closed_reports=0
        )
        await db.commit()
        return out

    handle = await moderation_repo.channel_handle(db, video.channel_id)
    video.moderation = state
    video.moderation_reason = new_reason
    closed = 0
    if state == ModerationState.hidden:
        closed = await moderation_repo.resolve_open_reports(db, video_id, actor_id)
    await moderation_repo.log(
        db,
        actor_id=actor_id,
        action=(
            ModerationAction.video_hide
            if state == ModerationState.hidden
            else ModerationAction.video_unhide
        ),
        target_id=video_id,
        reason=reason,
        details={
            "title": video.title,
            "channel_handle": handle,
            "closed_reports": closed,
            "own_video": owner_id == actor_id,  # админ по своему видео
        },
    )
    out = VideoModerationOut(
        video_id=video_id, moderation=state, moderation_reason=new_reason, closed_reports=closed
    )
    await db.commit()
    return out


async def dismiss_report(
    db: AsyncSession, actor: User, report_id: uuid.UUID, reason: str | None
) -> ReportDismissOut:
    actor_id = actor.id
    report = await moderation_repo.get_report(db, report_id)
    if report is None:
        raise NotFound(REPORT_NOT_FOUND)
    owner_id = await moderation_repo.report_target_owner(db, report)
    if not policies.can_moderate(actor, owner_id):
        raise Forbidden(OWN_VIDEO)
    if report.status != ReportStatus.open:
        raise Conflict(REPORT_CLOSED)
    if report.video_id is not None:
        # Видео → жалобы. Видео могли удалить, пока ждали: жалоба тогда уже с video_id NULL,
        # отклонить её всё равно можно.
        await videos_repo.lock_for_edit(db, report.video_id)
    dismissed = await moderation_repo.dismiss_open(db, report_id, actor_id)
    if dismissed is None:  # пока ждали, видео скрыли (жалоба resolved) или отклонил другой
        await db.rollback()
        raise Conflict(REPORT_CLOSED)
    await moderation_repo.log(
        db,
        actor_id=actor_id,
        action=ModerationAction.report_dismiss,
        target_id=report_id,
        reason=reason,
        details={
            "video_id": str(dismissed.video_id) if dismissed.video_id else None,
            "title": dismissed.video_title,
            "channel_handle": dismissed.channel_handle,
            "category": dismissed.category.value,
            "own_video": owner_id == actor_id,
        },
    )
    out = ReportDismissOut(
        id=dismissed.id,
        status=dismissed.status,
        resolved_by=dismissed.resolved_by,
        resolved_at=dismissed.resolved_at,
    )
    await db.commit()
    return out
