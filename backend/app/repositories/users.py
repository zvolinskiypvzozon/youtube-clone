import uuid

from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Channel, RetiredUsername, User, UserRole


async def get_by_login(db: AsyncSession, login: str) -> User | None:
    """Поиск по email (если есть @) или имени. citext — без учёта регистра."""
    column = User.email if "@" in login else User.username
    return (await db.execute(select(User).where(column == login))).scalar_one_or_none()


async def get_with_channel(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    stmt = select(User).where(User.id == user_id).options(selectinload(User.channel))
    return (await db.execute(stmt)).scalar_one_or_none()


async def email_taken(db: AsyncSession, email: str) -> bool:
    return bool(await db.scalar(select(exists().where(User.email == email))))


async def username_retired(db: AsyncSession, username: str) -> bool:
    """Ник удалённого пользователя — занят навсегда (citext, без учёта регистра)."""
    return bool(await db.scalar(select(exists().where(RetiredUsername.username == username))))


async def retire_username(db: AsyncSession, username: str) -> None:
    await db.execute(pg_insert(RetiredUsername).values(username=username).on_conflict_do_nothing())


async def username_taken(db: AsyncSession, username: str) -> bool:
    # handle канала = username, поэтому проверяем оба; плюс ники удалённых (день 9).
    return bool(
        await db.scalar(
            select(
                exists().where(User.username == username)
                | exists().where(Channel.handle == username)
                | exists().where(RetiredUsername.username == username)
            )
        )
    )


async def create_with_channel(
    db: AsyncSession, *, email: str, username: str, password_hash: str
) -> User:
    user = User(email=email, username=username, password_hash=password_hash)
    db.add(user)
    await db.flush()
    db.add(Channel(owner_id=user.id, handle=username, title=username))
    await db.flush()
    return user


async def get(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def list_page(db: AsyncSession, *, limit: int, offset: int) -> tuple[list[User], int]:
    total = await db.scalar(select(func.count()).select_from(User)) or 0
    stmt = select(User).order_by(User.created_at.desc(), User.id.desc()).limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars()), total


# Ключ транзакционной advisory-блокировки для смены ролей (произвольная константа).
ROLE_CHANGE_LOCK_ID = 7_100_001


async def lock_role_changes(db: AsyncSession) -> None:
    """Сериализует смены ролей; блокировка снимается при commit/rollback транзакции."""
    await db.execute(select(func.pg_advisory_xact_lock(ROLE_CHANGE_LOCK_ID)))


async def get_role(db: AsyncSession, user_id: uuid.UUID) -> UserRole | None:
    return await db.scalar(select(User.role).where(User.id == user_id))


async def get_active_role(db: AsyncSession, user_id: uuid.UUID) -> UserRole | None:
    """Роль инициатора, перечитанная под блокировкой смен ролей; None — удалён или забанен
    (его забанили, пока запрос ждал блокировку: прав больше нет)."""
    stmt = select(User.role).where(User.id == user_id, User.is_banned.is_(False))
    return await db.scalar(stmt)


async def count_admins(db: AsyncSession) -> int:
    return (
        await db.scalar(select(func.count()).select_from(User).where(User.role == UserRole.admin))
        or 0
    )


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    """citext — без учёта регистра."""
    return await db.scalar(select(User).where(User.username == username))


async def lock_active(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Своя строка пользователя FOR SHARE, если он не забанен (ADR 0006 доп. 8).

    Берётся первой в комментарии, реакции и подписке — порядок «пользователь → видео/канал»,
    как у бана. FOR SHARE конфликтует с UPDATE users при бане: бан, начавшийся раньше,
    заставит запрос дождаться его commit и увидеть is_banned (False → 401); запрос,
    начавшийся раньше, заставит бан ждать своего commit, и пересчёт счётчиков бана увидит его
    строку. Без этого строка забаненного могла бы попасть в счётчик мимо пересчёта."""
    stmt = (
        select(User.id)
        .where(User.id == user_id, User.is_banned.is_(False))
        .with_for_update(read=True)
    )
    return await db.scalar(stmt) is not None


async def lock_for_ban(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Строка цели бана или смены роли FOR NO KEY UPDATE (вызывать под lock_role_changes);
    populate_existing — роль и бан перечитываются, даже если объект уже в сессии.

    Не FOR UPDATE: ключ пользователя не меняется, а FOR UPDATE конфликтовал бы с FOR KEY
    SHARE проверок внешних ключей (жалоба, допуск, журнал, сессия со ссылкой на пользователя)
    — пути «видео → вставка со ссылкой на пользователя» взаимно блокировались бы с баном
    «пользователь → видео» (ревью и аудит шага 3). С FOR SHARE из lock_active конфликт
    остаётся — на нём держится защита счётчиков."""
    stmt = (
        select(User)
        .where(User.id == user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return await db.scalar(stmt)


async def set_local_statement_timeout(db: AsyncSession, ms: int) -> None:
    """statement_timeout до конца текущей транзакции (SET LOCAL; транзакция начинается этим
    запросом). Значение — целое из кода, не ввод пользователя."""
    await db.execute(text(f"SET LOCAL statement_timeout = {int(ms)}"))


async def lock_self_exclusive(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Своя строка FOR NO KEY UPDATE, если пользователь не забанен: сериализует проверки
    потолков «на пользователя» (открытые жалобы) между его параллельными запросами. Берётся до
    строки видео — порядок «пользователь → видео», как у бана (ADR 0006 доп. 8)."""
    stmt = (
        select(User.id)
        .where(User.id == user_id, User.is_banned.is_(False))
        .with_for_update(key_share=True)
    )
    return await db.scalar(stmt) is not None


async def lock_channel_of(db: AsyncSession, user_id: uuid.UUID) -> Channel | None:
    """Канал пользователя FOR UPDATE: удаление пользователя держит его до commit, поэтому
    новая загрузка (старт блокирует строку канала — лимит «в работе») не появится между
    удалением видео и удалением пользователя."""
    stmt = select(Channel).where(Channel.owner_id == user_id).with_for_update()
    return await db.scalar(stmt)


async def delete_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Каскадом (ON DELETE CASCADE): канал и его плейлисты, сессии, реакции, комментарии,
    подписки (свои и на свой канал), допуски. Видео к этому моменту уже удалены
    (services/accounts — через services/deletion, порядок «задачи → видео»)."""
    await db.execute(delete(User).where(User.id == user_id))
