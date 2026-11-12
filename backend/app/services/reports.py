"""Жалобы на видео (ADR 0006 доп. 6).

Пожаловаться можно на видео, которое пользователь видит (`can_view`, иначе 404) и с которым
можно взаимодействовать (`can_interact`, иначе 409: скрытое уже снято, необработанное ещё
не показывается). На своё видео — 400. Одна открытая жалоба пользователя на видео — 409.

Жалобы целиком видит только staff (очередь модерации): владелец видео не узнаёт, кто
пожаловался, — ему видна только причина скрытия (`moderation_reason`).

Порядок блокировок «видео → жалобы»: жалоба берёт строку видео FOR SHARE с условиями политики
в самом запросе, затем вставляет строку жалобы. Скрытие держит строку видео (FOR NO KEY
UPDATE) и закрывает открытые жалобы; удаление видео держит её (FOR UPDATE) и каскадом
обнуляет reports.video_id. Жалоба, пришедшая во время скрытия, ждёт его commit и получает 409.
Перед строкой видео — своя строка пользователя (потолок открытых жалоб, шаг 2 дня 6): полный
порядок «пользователь → видео → жалобы».
"""

import uuid

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, Busy, Conflict, Unauthorized
from app.models import User
from app.repositories import reports as reports_repo
from app.repositories import users as users_repo
from app.schemas.moderation import ReportCreatedOut, ReportIn
from app.services import bans, policies
from app.services import videos as videos_service

SELF_REPORT = "Нельзя пожаловаться на своё видео"
ALREADY_REPORTED = "Вы уже пожаловались на это видео — жалоба на рассмотрении"
# Потолок открытых жалоб пользователя (решение архитектора 2026-11-10, шаг 2): вместе с зоной
# limit_req `reports` не даёт засорить очередь модерации. Охлаждения после отклонения нет.
OPEN_REPORTS_LIMIT = 20


def open_reports_limit_reached() -> str:
    return f"У вас уже {OPEN_REPORTS_LIMIT} жалоб на рассмотрении — дождитесь решения модератора"


VIDEO_CHANGING = "Видео сейчас изменяется. Повторите попытку через несколько секунд"


async def create_report(
    db: AsyncSession, viewer: User, video_id: uuid.UUID, data: ReportIn
) -> ReportCreatedOut:
    viewable = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    if viewable.channel.owner_id == viewer.id:
        raise BadRequest(SELF_REPORT)
    # готово, не скрыто, автор видео не забанен — иначе 409
    videos_service.ensure_interactive(viewable.video, owner_banned=viewable.owner_banned)
    # Повтор — отказ без блокировки строки видео: иначе поток повторных жалоб держал бы
    # FOR SHARE и не давал скрыть, править или удалить видео (аудит шага 4, Medium).
    if await reports_repo.has_open(db, video_id, viewer.id):
        raise Conflict(ALREADY_REPORTED)

    # Потолок открытых жалоб — под блокировкой своей строки (FOR NO KEY UPDATE): две жалобы
    # на разные видео иначе обе увидели бы 19. Порядок «пользователь → видео», как у бана.
    viewer_id = viewer.id  # до rollback: он просрочит объект
    if not await users_repo.lock_self_exclusive(db, viewer_id):  # забанен, пока шёл запрос
        await db.rollback()
        raise Unauthorized(bans.LOGIN_REQUIRED)
    if await reports_repo.count_open_by(db, viewer_id) >= OPEN_REPORTS_LIMIT:
        await db.rollback()
        raise Conflict(open_reports_limit_reached())
    where = and_(policies.visible_filter(viewer), policies.interactable_filter())
    locked = await reports_repo.share_lock_video(db, video_id, where)
    if locked is None:
        # Пока ждали блокировку, видео удалили, сделали недоступным или скрыли: причина —
        # по свежему состоянию (404 или 409); если оно уже вернулось обратно — повторить.
        db.expire(viewable.video)
        viewable = await videos_service.get_viewable(db, viewer, video_id)
        videos_service.ensure_interactive(viewable.video, owner_banned=viewable.owner_banned)
        raise Busy(VIDEO_CHANGING)
    video, channel = locked

    report = await reports_repo.create_open(
        db,
        video_id=video.id,
        reporter_id=viewer.id,
        category=data.category,
        comment=data.comment,
        video_title=video.title,  # снимок: переживёт удаление видео
        channel_handle=channel.handle,
        channel_id=channel.id,  # снимок без FK: «своё видео» модератора после удаления видео
    )
    if report is None:
        await db.rollback()
        raise Conflict(ALREADY_REPORTED)
    out = ReportCreatedOut.model_validate(report)
    await db.commit()
    return out
