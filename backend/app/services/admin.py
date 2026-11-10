"""Администрирование пользователей: список и смена роли (только роль admin).

Смена роли пишется в журнал модерации (`role_change`, из какой роли в какую). Забаненному
роль не меняется (409): иначе можно было бы получить забаненного админа и обойти правило
«админа не банят» (ADR 0006 доп. 8)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound
from app.models import ModerationAction, User, UserRole
from app.repositories import moderation as moderation_repo
from app.repositories import users as users_repo

BANNED_ROLE_CHANGE = "Пользователь заблокирован: сначала разблокируйте его"


async def list_users(db: AsyncSession, *, limit: int, offset: int) -> tuple[list[User], int]:
    users, total = await users_repo.list_page(db, limit=limit, offset=offset)
    await db.commit()  # только чтение; commit не делает объекты устаревшими
    return users, total


async def set_role(db: AsyncSession, actor: User, user_id: uuid.UUID, role: UserRole) -> User:
    """Смена роли. Инвариант: в системе всегда остаётся хотя бы один администратор.

    Все смены ролей сериализуются транзакционной блокировкой. Под ней заново читается роль
    инициатора: два администратора, одновременно понижающие друг друга, иначе оба прошли бы
    проверку роли (прочитанную до чужого commit) и оставили систему без администраторов.
    """
    actor_id = actor.id  # до rollback: он просрочит объект
    await users_repo.lock_role_changes(db)
    try:
        actor_role = await users_repo.get_active_role(db, actor_id)
        if actor_role != UserRole.admin:
            raise Forbidden("Недостаточно прав")
        if user_id == actor_id and role != UserRole.admin:
            raise Conflict("Нельзя понизить собственную роль")

        user = await users_repo.lock_for_ban(db, user_id)  # роль и бан — свежие, под блокировкой
        if user is None:
            raise NotFound("Пользователь не найден")
        if user.is_banned:
            raise Conflict(BANNED_ROLE_CHANGE)
        demotes_admin = user.role == UserRole.admin and role != UserRole.admin
        if demotes_admin and await users_repo.count_admins(db) <= 1:
            raise Conflict("Нельзя снять последнего администратора")
        previous = user.role
        if previous != role:
            user.role = role
            await moderation_repo.log(
                db,
                actor_id=actor_id,
                action=ModerationAction.role_change,
                target_id=user.id,
                details={"username": user.username, "from": previous.value, "to": role.value},
            )
        await db.commit()
        return user
    except Exception:
        await db.rollback()  # снимает блокировку
        raise
