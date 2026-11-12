"""Удаление пользователя (день 9): сам — с паролем, админ — user и moderator, последнего
админа — никак; каскад, файлы, счётчики, освобождённый ник, жалобы и журнал."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.models import (
    Channel,
    ModerationAction,
    ModerationLogEntry,
    Playlist,
    Report,
    RetiredUsername,
    User,
    UserRole,
    UserSession,
    Video,
)
from tests.helpers import PASSWORD, api_user, channel_of, seed_video

UID = uuid.UUID


async def _count(db: AsyncSession, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def _delete_me(client: AsyncClient, user: dict, password: str = PASSWORD):
    return await client.request(
        "DELETE", "/api/me", json={"password": password}, headers=user["auth"]
    )


async def _media_keys(video_id: uuid.UUID) -> list:
    bucket = get_settings().s3_bucket_media
    return await asyncio.to_thread(storage.list_objects, bucket, f"videos/{video_id}/")


async def test_self_delete_removes_everything_and_frees_nothing(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner = await api_user(client, db)
    fan = await api_user(client, db)
    other = await api_user(client, db)
    own_video = await seed_video(db, await channel_of(db, owner))
    other_video = await seed_video(db, await channel_of(db, other))
    own_id = own_video.id
    assert await _media_keys(own_video.id)
    # Плейлист владельца, чужой лайк и комментарий под его видео.
    r = await client.post(
        "/api/playlists",
        json={"title": "Мой", "visibility": "public"},
        headers=owner["auth"],
    )
    assert r.status_code == 201
    await client.put(
        f"/api/videos/{own_video.id}/reaction", json={"kind": "like"}, headers=fan["auth"]
    )
    # Сам владелец лайкает, комментирует и подписывается на чужое — счётчики пересчитаются.
    await client.put(
        f"/api/videos/{other_video.id}/reaction", json={"kind": "like"}, headers=owner["auth"]
    )
    await client.post(
        f"/api/videos/{other_video.id}/comments", json={"body": "привет"}, headers=owner["auth"]
    )
    await client.put(f"/api/channels/{other['username']}/subscription", headers=owner["auth"])
    await db.refresh(other_video)
    assert (other_video.likes_count, other_video.comments_count) == (1, 1)

    r = await _delete_me(client, owner)
    assert r.status_code == 204, r.text
    # Cookie сессии очищаются.
    assert any(
        "max-age=0" in v.lower() or "expires=" in v.lower()
        for v in r.headers.get_list("set-cookie")
    )

    owner_id = UID(owner["id"])
    db.expire_all()
    assert await _count(db, User, User.id == owner_id) == 0
    assert await _count(db, Channel, Channel.owner_id == owner_id) == 0
    assert await _count(db, Video, Video.id == own_id) == 0
    assert await _count(db, Playlist, Playlist.title == "Мой") == 0
    assert await _count(db, UserSession, UserSession.user_id == owner_id) == 0
    assert await _media_keys(own_id) == []
    # Счётчики чужого видео и канала — без удалённого.
    await db.refresh(other_video)
    assert (other_video.likes_count, other_video.comments_count) == (0, 0)
    other_channel = await channel_of(db, other)
    assert other_channel.subscribers_count == 0
    # Токен больше не действует.
    assert (await client.get("/api/auth/me", headers=owner["auth"])).status_code == 401


async def test_wrong_password_is_403_and_nothing_deleted(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = await api_user(client, db)
    r = await _delete_me(client, user, "wrong-password")
    assert r.status_code == 403
    assert await _count(db, User, User.id == UID(user["id"])) == 1


async def test_username_retired_forever_email_reusable(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = await api_user(client, db)
    email = await db.scalar(select(User.email).where(User.id == UID(user["id"])))
    assert (await _delete_me(client, user)).status_code == 204
    assert await _count(db, RetiredUsername, RetiredUsername.username == user["username"]) == 1
    # Тот же ник — в любом регистре — занят навсегда.
    for name in (user["username"], user["username"].upper()):
        r = await client.post(
            "/api/auth/register",
            json={
                "email": f"x_{uuid.uuid4().hex[:6]}@example.com",
                "username": name,
                "password": PASSWORD,
            },
        )
        assert r.status_code == 409, r.text
    # Тот же email под другим ником — можно (email не хранится).
    client.cookies.clear()
    r = await client.post(
        "/api/auth/register",
        json={"email": email, "username": f"n_{uuid.uuid4().hex[:6]}", "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    # Хранится только ник: в таблице нет других колонок с персональными данными.
    assert {c.name for c in RetiredUsername.__table__.columns} == {"username", "retired_at"}


@pytest.mark.parametrize(
    "actor_role,target_role,expected",
    [
        (UserRole.admin, UserRole.user, 204),
        (UserRole.admin, UserRole.moderator, 204),
        (UserRole.admin, UserRole.admin, 403),
        (UserRole.moderator, UserRole.user, 403),
        (UserRole.user, UserRole.user, 403),
    ],
)
async def test_admin_route_matrix(
    client: AsyncClient,
    db: AsyncSession,
    actor_role: UserRole,
    target_role: UserRole,
    expected: int,
) -> None:
    actor = await api_user(client, db, actor_role)
    target = await api_user(client, db, target_role)
    r = await client.delete(f"/api/admin/users/{target['id']}", headers=actor["auth"])
    assert r.status_code == expected, r.text
    exists = await _count(db, User, User.id == UID(target["id"]))
    assert exists == (0 if expected == 204 else 1)
    if expected == 204:
        entry = await db.scalar(
            select(ModerationLogEntry).where(ModerationLogEntry.target_id == UID(target["id"]))
        )
        assert entry is not None and entry.action == ModerationAction.user_delete
        assert entry.details["username"] == target["username"]


async def test_admin_cannot_delete_self_via_admin_route(
    client: AsyncClient, db: AsyncSession
) -> None:
    await api_user(client, db, UserRole.admin)  # второй админ: дело не в «последнем»
    admin = await api_user(client, db, UserRole.admin)
    r = await client.delete(f"/api/admin/users/{admin['id']}", headers=admin["auth"])
    assert r.status_code == 403


async def test_missing_user_is_404(client: AsyncClient, db: AsyncSession) -> None:
    admin = await api_user(client, db, UserRole.admin)
    r = await client.delete(f"/api/admin/users/{uuid.uuid4()}", headers=admin["auth"])
    assert r.status_code == 404


async def test_last_admin_cannot_delete_self(client: AsyncClient, db: AsyncSession) -> None:
    admins = (await db.execute(select(User).where(User.role == UserRole.admin))).scalars().all()
    for a in admins:  # в тестовой БД мог остаться bootstrap-админ — делаем нашего единственным
        a.role = UserRole.user
    await db.commit()
    admin = await api_user(client, db, UserRole.admin)
    r = await _delete_me(client, admin)
    assert r.status_code == 409
    second = await api_user(client, db, UserRole.admin)
    assert (await _delete_me(client, second)).status_code == 204
    assert (await _delete_me(client, admin)).status_code == 409


async def test_reports_and_log_survive_deletion(client: AsyncClient, db: AsyncSession) -> None:
    author = await api_user(client, db)
    reporter = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, author))
    r = await client.post(
        f"/api/videos/{video.id}/reports", json={"category": "spam"}, headers=reporter["auth"]
    )
    assert r.status_code == 201
    channel_id = (await channel_of(db, author)).id

    assert (await _delete_me(client, reporter)).status_code == 204
    assert (await _delete_me(client, author)).status_code == 204
    db.expire_all()
    report = await db.scalar(select(Report).where(Report.channel_handle == author["username"]))
    assert report is not None
    assert report.reporter_id is None and report.video_id is None
    assert report.channel_id == channel_id  # снимок канала остался


async def test_moderator_queue_hides_own_deleted_video_by_channel_id(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Жалоба на удалённое видео модератора: «своё» — по снимку channel_id, не по handle."""
    moderator = await api_user(client, db, UserRole.moderator)
    reporter = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, moderator))
    r = await client.post(
        f"/api/videos/{video.id}/reports", json={"category": "spam"}, headers=reporter["auth"]
    )
    assert r.status_code == 201
    report_id = r.json()["id"]
    assert (
        await client.delete(f"/api/videos/{video.id}", headers=moderator["auth"])
    ).status_code == 204
    # Снимок handle испорчен: проверка обязана идти по channel_id.
    report = await db.get(Report, UID(report_id))
    report.channel_handle = "someone_else"
    await db.commit()

    queue = await client.get("/api/admin/reports", headers=moderator["auth"])
    assert report_id not in {item["id"] for item in queue.json()["items"]}
    r = await client.post(f"/api/admin/reports/{report_id}/dismiss", headers=moderator["auth"])
    assert r.status_code == 403


async def _video_count(db: AsyncSession, user: dict) -> int:
    channel = await channel_of(db, user)
    return await _count(db, Video, Video.channel_id == channel.id)


@pytest.mark.parametrize("case", ["admin_target", "self_via_admin_route", "last_admin_self"])
async def test_refusal_happens_before_videos_are_deleted(
    client: AsyncClient, db: AsyncSession, case: str
) -> None:
    """403/409 — до фазы удаления видео: контент цели цел (аудит дня 9, High и Medium)."""
    if case == "last_admin_self":
        admins = (await db.execute(select(User).where(User.role == UserRole.admin))).scalars().all()
        for a in admins:
            a.role = UserRole.user
        await db.commit()
    actor = await api_user(client, db, UserRole.admin)
    target = {"admin_target": None, "self_via_admin_route": actor, "last_admin_self": actor}[case]
    if target is None:
        target = await api_user(client, db, UserRole.admin)
    video = await seed_video(db, await channel_of(db, target))
    video_id = video.id
    assert await _media_keys(video_id)

    if case == "last_admin_self":
        r = await _delete_me(client, actor)
        assert r.status_code == 409
    else:
        r = await client.delete(f"/api/admin/users/{target['id']}", headers=actor["auth"])
        assert r.status_code == 403
    db.expire_all()
    assert await _video_count(db, target) == 1
    assert await _media_keys(video_id)
