"""Демо-учётки сида (app/seed_users.py): создание, повтор без дублей, конфликты."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import verify_password
from app.models import Channel, User, UserRole
from app.seed_users import DEMO_USERS, SeedError, ensure_demo_users


def _passwords(suffix: str) -> dict[str, str]:
    return {u.username: f"{u.username}-{suffix}-password" for u in DEMO_USERS}


async def _demo(db: AsyncSession) -> dict[str, User]:
    names = [u.username for u in DEMO_USERS]
    users = (await db.execute(select(User).where(User.username.in_(names)))).scalars().all()
    return {u.username: u for u in users}


async def test_creates_demo_users_with_roles_and_channels(db: AsyncSession) -> None:
    result = await ensure_demo_users(get_settings().database_url, _passwords("a"))
    assert result == {u.username: "создан" for u in DEMO_USERS}
    users = await _demo(db)
    assert {name: u.role for name, u in users.items()} == {
        "demo_admin": UserRole.admin,
        "demo_moderator": UserRole.moderator,
        "demo_user": UserRole.user,
        "demo_author": UserRole.user,
    }
    for demo in DEMO_USERS:
        user = users[demo.username]
        assert verify_password(user.password_hash, f"{demo.username}-a-password")
        assert user.email == f"{demo.username}@demo.invalid"
        channels = await db.scalar(
            select(func.count()).select_from(Channel).where(Channel.owner_id == user.id)
        )
        assert channels == 1


async def test_rerun_updates_passwords_and_roles_without_duplicates(db: AsyncSession) -> None:
    url = get_settings().database_url
    await ensure_demo_users(url, _passwords("a"))
    # Роль сменили руками (например, демо-пользователю выдали модератора) — сид вернёт её.
    users = await _demo(db)
    users["demo_user"].role = UserRole.moderator
    await db.commit()

    result = await ensure_demo_users(url, _passwords("b"))
    assert result == {u.username: "обновлён" for u in DEMO_USERS}
    db.expire_all()
    users = await _demo(db)
    assert len(users) == len(DEMO_USERS)
    assert users["demo_user"].role == UserRole.user
    for demo in DEMO_USERS:
        assert verify_password(users[demo.username].password_hash, f"{demo.username}-b-password")
    total = await db.scalar(
        select(func.count()).select_from(Channel).where(Channel.handle.like("demo\\_%"))
    )
    assert total == len(DEMO_USERS)


async def test_conflict_with_foreign_account_fails_without_partial_set(db: AsyncSession) -> None:
    """Ник demo_author занят чужим аккаунтом → ошибка, ни одна демо-учётка не создана."""
    db.add(User(email="someone@example.com", username="demo_author", password_hash="x"))
    await db.commit()
    with pytest.raises(SeedError, match="demo_author"):
        await ensure_demo_users(get_settings().database_url, _passwords("a"))
    db.expire_all()
    users = await _demo(db)
    assert set(users) == {"demo_author"}
    assert users["demo_author"].email == "someone@example.com"


async def test_conflict_by_email_fails(db: AsyncSession) -> None:
    db.add(User(email="demo_user@demo.invalid", username="other_name", password_hash="x"))
    await db.commit()
    with pytest.raises(SeedError, match="demo_user"):
        await ensure_demo_users(get_settings().database_url, _passwords("a"))


async def test_conflict_by_channel_handle_fails(db: AsyncSession) -> None:
    owner = User(email="o@example.com", username="owner_x", password_hash="x")
    db.add(owner)
    await db.flush()
    db.add(Channel(owner_id=owner.id, handle="demo_admin", title="чужой"))
    await db.commit()
    with pytest.raises(SeedError, match="имя канала"):
        await ensure_demo_users(get_settings().database_url, _passwords("a"))


@pytest.mark.parametrize(
    "passwords,message",
    [
        ({"demo_admin": "long-enough-password"}, "нет паролей"),
        ({u.username: "short" for u in DEMO_USERS}, "короче"),
    ],
)
async def test_validates_input(passwords: dict[str, str], message: str) -> None:
    with pytest.raises(SeedError, match=message):
        await ensure_demo_users(get_settings().database_url, passwords)


async def test_rerun_unbans_demo_user_and_recounts(db: AsyncSession) -> None:
    """Демо-учётку забанили при проверке → повторный сид снимает бан и пересчитывает счётчики
    (иначе вход не проходит и каждый следующий деплой падает — ревью дня 9)."""
    from app.models import ReactionKind, VideoStatus
    from app.models.video import Reaction
    from tests.factories import create_user, create_video

    url = get_settings().database_url
    await ensure_demo_users(url, _passwords("a"))
    users = await _demo(db)
    author = await create_user(db)
    channel = await db.scalar(select(Channel).where(Channel.owner_id == author.id))
    video = await create_video(db, channel, status=VideoStatus.ready)
    db.add(Reaction(video_id=video.id, user_id=users["demo_user"].id, kind=ReactionKind.like))
    # Забанен: лайк не учитывается в счётчике (как после бана через API).
    users["demo_user"].is_banned = True
    users["demo_user"].banned_reason = "проверка"
    users["demo_user"].banned_by_role = UserRole.moderator
    video.likes_count = 0
    await db.commit()

    result = await ensure_demo_users(url, _passwords("b"))
    assert result["demo_user"] == "обновлён, бан снят"
    db.expire_all()
    users = await _demo(db)
    assert users["demo_user"].is_banned is False
    assert users["demo_user"].banned_reason is None
    assert users["demo_user"].banned_by_role is None
    await db.refresh(video)
    assert video.likes_count == 1
