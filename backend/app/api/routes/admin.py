import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import Cursor, DbSession, Limit, no_store, require_role
from app.models import ReportStatus, User, UserRole
from app.schemas.admin import (
    AdminUserOut,
    AdminUserPage,
    BanIn,
    BanOut,
    ReportDismissIn,
    ReportDismissOut,
    ReportQueuePage,
    RoleUpdateIn,
    VideoModerationIn,
    VideoModerationOut,
)
from app.schemas.common import err
from app.services import admin as admin_service
from app.services import bans as bans_service
from app.services import moderation as moderation_service
from app.services import policies

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(no_store)],  # email и роли пользователей — не кэшировать
    responses={401: err("Требуется вход"), 403: err("Недостаточно прав")},
)

AdminUser = Annotated[User, Depends(require_role(UserRole.admin))]
# Модерация — staff (moderator и admin): тот же набор ролей, что в policies.is_staff.
StaffUser = Annotated[User, Depends(require_role(*policies.STAFF_ROLES))]


@router.get("/users", response_model=AdminUserPage, summary="Список пользователей")
async def list_users(
    _: AdminUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminUserPage:
    users, total = await admin_service.list_users(db, limit=limit, offset=offset)
    return AdminUserPage(
        items=[AdminUserOut.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="Сменить роль пользователя (user / moderator / admin)",
    responses={
        404: err("Пользователь не найден"),
        409: err("Своя роль, последний админ или пользователь заблокирован"),
    },
)
async def set_role(
    user_id: uuid.UUID, data: RoleUpdateIn, actor: AdminUser, db: DbSession
) -> AdminUserOut:
    user = await admin_service.set_role(db, actor, user_id, data.role)
    return AdminUserOut.model_validate(user)


# --- Бан (moderator и admin; кто кого — policies.can_ban / can_unban) ----------------------

_BAN_ERRORS = {
    403: err("Недостаточно прав: роль цели, сам себя или бан, наложенный админом"),
    404: err("Пользователь не найден"),
    503: err("Данные пользователя сейчас изменяются — повторить (Retry-After)"),
}


def _ban_out(result: bans_service.BanResult) -> BanOut:
    u = result.user
    return BanOut(
        user_id=u.id,
        username=u.username,
        role=u.role,
        is_banned=u.is_banned,
        banned_reason=u.banned_reason,
        banned_by_role=u.banned_by_role,
        recount_videos=result.recount.videos,
        recount_channels=result.recount.channels,
    )


@router.put(
    "/users/{user_id}/ban",
    response_model=BanOut,
    summary="Заблокировать пользователя (staff)",
    description=(
        "Модератор банит только пользователей, админ — пользователей и модераторов; админа и "
        "себя забанить нельзя. Контент заблокированного скрывается (видит только staff), "
        "его реакции, комментарии и подписки исключаются из счётчиков, сессии удаляются."
    ),
    responses={**_BAN_ERRORS, 409: err("Пользователь уже заблокирован")},
)
async def ban_user(user_id: uuid.UUID, data: BanIn, actor: StaffUser, db: DbSession) -> BanOut:
    return _ban_out(await bans_service.ban(db, actor, user_id, data.reason))


@router.delete(
    "/users/{user_id}/ban",
    response_model=BanOut,
    summary="Разблокировать пользователя (staff)",
    description="Бан, наложенный админом, снимает только админ. Счётчики пересчитываются.",
    responses={**_BAN_ERRORS, 409: err("Пользователь не заблокирован")},
)
async def unban_user(user_id: uuid.UUID, actor: StaffUser, db: DbSession) -> BanOut:
    return _ban_out(await bans_service.unban(db, actor, user_id))


# --- Модерация (moderator и admin) ----------------------------------------------------------


@router.get(
    "/reports",
    response_model=ReportQueuePage,
    summary="Очередь жалоб (staff), новые сверху",
    description=(
        "Жалобы видит только staff: кто пожаловался, владелец видео не узнаёт. "
        "Модератору жалобы на его собственные видео не показываются."
    ),
    responses={400: err("Некорректный курсор")},
)
async def list_reports(
    actor: StaffUser,
    db: DbSession,
    status: ReportStatus = ReportStatus.open,
    cursor: Cursor = None,
    limit: Limit = 20,
) -> ReportQueuePage:
    return await moderation_service.list_reports(db, actor, status, cursor, limit)


@router.post(
    "/reports/{report_id}/dismiss",
    response_model=ReportDismissOut,
    summary="Отклонить жалобу (staff)",
    responses={
        403: err("Недостаточно прав или жалоба на своё видео (модератор)"),
        404: err("Жалоба не найдена"),
        409: err("Жалоба уже закрыта"),
        503: err("Видео сейчас изменяется — повторить (Retry-After)"),
    },
)
async def dismiss_report(
    report_id: uuid.UUID, actor: StaffUser, db: DbSession, data: ReportDismissIn | None = None
) -> ReportDismissOut:
    reason = data.reason if data else None
    return await moderation_service.dismiss_report(db, actor, report_id, reason)


@router.put(
    "/videos/{video_id}/moderation",
    response_model=VideoModerationOut,
    summary="Скрыть видео или вернуть его (staff; модератор — только чужое)",
    description=(
        "Скрытие: причину видит владелец, открытые жалобы на видео закрываются. "
        "Возврат: причина снимается, закрытые жалобы не открываются."
    ),
    responses={
        403: err("Недостаточно прав или своё видео (модератор)"),
        404: err("Видео не найдено"),
        503: err("Видео сейчас изменяется — повторить (Retry-After)"),
    },
)
async def set_video_moderation(
    video_id: uuid.UUID, data: VideoModerationIn, actor: StaffUser, db: DbSession
) -> VideoModerationOut:
    return await moderation_service.set_video_moderation(
        db, actor, video_id, data.state, data.reason
    )
