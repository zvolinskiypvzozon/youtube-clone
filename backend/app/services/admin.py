"""Администрирование пользователей: список и смена роли (только роль admin)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound
from app.models import User, UserRole
from app.repositories import users as users_repo


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
    await users_repo.lock_role_changes(db)
    try:
        actor_role = await users_repo.get_role(db, actor.id)
        if actor_role != UserRole.admin:
            raise Forbidden("Недостаточно прав")
        if user_id == actor.id and role != UserRole.admin:
            raise Conflict("Нельзя понизить собственную роль")

        user = await users_repo.get(db, user_id)
        if user is None:
            raise NotFound("Пользователь не найден")
        demotes_admin = user.role == UserRole.admin and role != UserRole.admin
        if demotes_admin and await users_repo.count_admins(db) <= 1:
            raise Conflict("Нельзя снять последнего администратора")
        user.role = role
        await db.commit()
        return user
    except Exception:
        await db.rollback()  # снимает блокировку
        raise
