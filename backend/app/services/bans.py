"""Бан и разбан пользователя (ADR 0006 доп. 8).

Кто кого — policies.can_ban / can_unban: модератор — только user; админ — user и moderator;
админа и себя не банит никто; бан, наложенный админом, модератор не снимает. Все отказы —
403. Повтор (бан забаненного, разбан незабаненного) — 409.

Контент забаненного не удаляется, а скрывается политикой (видит только staff). Его реакции,
комментарии и подписки исключаются из счётчиков полным пересчётом — при бане и при разбане
одной функцией `recount_for_user` (её же вызовет удаление пользователя, день 9).

Порядок блокировок: advisory-блокировка смен ролей (сериализует бан, разбан и смену роли;
роли инициатора и цели перечитываются под ней) → строка цели FOR UPDATE → UPDATE users и
DELETE sessions → затронутые видео (по id) → затронутые каналы (по id) → журнал.
Комментарий, реакция и подписка берут «пользователь (FOR SHARE) → видео/канал» —
`lock_self_active`, тот же порядок, поэтому цикла нет.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized
from app.models import ModerationAction, User, UserRole
from app.repositories import counters as counters_repo
from app.repositories import moderation as moderation_repo
from app.repositories import sessions as sessions_repo
from app.repositories import users as users_repo
from app.services import policies

USER_NOT_FOUND = "Пользователь не найден"
CANNOT_BAN = "Недостаточно прав, чтобы заблокировать этого пользователя"
CANNOT_UNBAN = "Недостаточно прав, чтобы разблокировать этого пользователя"
ALREADY_BANNED = "Пользователь уже заблокирован"
NOT_BANNED = "Пользователь не заблокирован"
LOGIN_REQUIRED = "Требуется вход"
# Пересчёт счётчиков у активного пользователя бывает долгим; бан — редкое действие staff,
# поэтому ему — свой предел вместо общих 5 с API (решение архитектора 2026-11-10, шаг 2).
BAN_STATEMENT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class Recount:
    videos: int
    channels: int


@dataclass(frozen=True)
class BanResult:
    user: User
    recount: Recount


async def lock_self_active(db: AsyncSession, viewer: User) -> None:
    """Перед изменением счётчика (комментарий, реакция, подписка): своя строка FOR SHARE,
    если зритель не забанен. Забанен, пока запрос шёл, — откат и 401, как для любого
    запроса забаненного (auth.authenticate)."""
    viewer_id = viewer.id  # до rollback: он просрочит объект
    if not await users_repo.lock_active(db, viewer_id):
        await db.rollback()
        raise Unauthorized(LOGIN_REQUIRED)


async def recount_for_user(db: AsyncSession, user_id: uuid.UUID) -> Recount:
    """Счётчики видео и каналов, которых касались реакции, комментарии и подписки
    пользователя, — заново по строкам незабаненных. Строки блокируются по id."""
    video_ids = await counters_repo.videos_touched_by(db, user_id)
    await counters_repo.recount_videos(db, video_ids)
    channel_ids = await counters_repo.channels_subscribed_by(db, user_id)
    await counters_repo.recount_channels(db, channel_ids)
    return Recount(videos=len(video_ids), channels=len(channel_ids))


async def _lock_target(
    db: AsyncSession, actor: User, user_id: uuid.UUID
) -> tuple[UserRole | None, User]:
    """Под блокировкой смен ролей: роль инициатора (перечитанная) и строка цели.
    Первым запросом транзакции — свой statement_timeout для бана и разбана."""
    await users_repo.set_local_statement_timeout(db, BAN_STATEMENT_TIMEOUT_MS)
    await users_repo.lock_role_changes(db)
    actor_role = await users_repo.get_active_role(db, actor.id)
    target = await users_repo.lock_for_ban(db, user_id)
    if target is None:
        raise NotFound(USER_NOT_FOUND)
    return actor_role, target


async def ban(db: AsyncSession, actor: User, user_id: uuid.UUID, reason: str) -> BanResult:
    actor_id = actor.id
    try:
        actor_role, target = await _lock_target(db, actor, user_id)
        if not policies.can_ban(actor_id, actor_role, target.id, target.role):
            raise Forbidden(CANNOT_BAN)
        if target.is_banned:
            raise Conflict(ALREADY_BANNED)
        target.is_banned = True
        target.banned_reason = reason
        target.banned_by_role = actor_role
        await db.flush()  # бан виден следующим запросам транзакции (пересчёт)
        await sessions_repo.delete_all_for_user(db, target.id)
        recount = await recount_for_user(db, target.id)
        await moderation_repo.log(
            db,
            actor_id=actor_id,
            action=ModerationAction.user_ban,
            target_id=target.id,
            reason=reason,
            details={
                "username": target.username,
                "role": target.role.value,
                "by_role": actor_role.value if actor_role else None,
                "recount_videos": recount.videos,
                "recount_channels": recount.channels,
            },
        )
        await db.commit()
        return BanResult(target, recount)
    except Exception:
        await db.rollback()  # снимает блокировки
        raise


async def unban(db: AsyncSession, actor: User, user_id: uuid.UUID) -> BanResult:
    """Разбан: тот же пересчёт — реакции, комментарии и подписки снова учитываются.
    Сессии не восстанавливаются: пользователь входит заново."""
    actor_id = actor.id
    try:
        actor_role, target = await _lock_target(db, actor, user_id)
        if not policies.can_unban(
            actor_id, actor_role, target.id, target.role, target.banned_by_role
        ):
            raise Forbidden(CANNOT_UNBAN)
        if not target.is_banned:
            raise Conflict(NOT_BANNED)
        previous = {"reason": target.banned_reason, "by_role": target.banned_by_role}
        target.is_banned = False
        target.banned_reason = None
        target.banned_by_role = None
        await db.flush()
        recount = await recount_for_user(db, target.id)
        await moderation_repo.log(
            db,
            actor_id=actor_id,
            action=ModerationAction.user_unban,
            target_id=target.id,
            details={
                "username": target.username,
                "role": target.role.value,
                "banned_reason": previous["reason"],
                "banned_by_role": previous["by_role"].value if previous["by_role"] else None,
                "recount_videos": recount.videos,
                "recount_channels": recount.channels,
            },
        )
        await db.commit()
        return BanResult(target, recount)
    except Exception:
        await db.rollback()
        raise
