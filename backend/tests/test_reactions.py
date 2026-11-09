"""Лайки и дизлайки (K6): идемпотентность, взаимоисключение, сходимость счётчиков, доступ.

Доступ — can_view (ADR 0006): реакции только тем, кто может видеть видео, и только к готовому.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.models import Reaction, ReactionKind, User, UserRole, Video
from app.repositories import videos as videos_repo
from app.schemas.videos import VideoUpdateIn
from app.services import manage as manage_service
from app.services import reactions as reactions_service
from app.services import videos as videos_service
from tests.helpers import (
    HIDDEN,
    MANAGE_OPENS,
    MANAGE_STATES,
    MANAGE_VIEWERS,
    PRIV,
    PUB,
    READY,
    api_user,
    auth_headers,
    backend_pid,
    channel_of,
    manage_matrix,
    seed_video,
    wait_for_lock_waiters,
)

# Видят, но взаимодействовать нельзя (policies.can_interact): не готово или скрыто модератором.
NOT_INTERACTIVE_STATES = {"processing", "failed", "hidden"}


def _url(video_id: uuid.UUID) -> str:
    return f"/api/videos/{video_id}/reaction"


async def _counts(db: AsyncSession, video_id: uuid.UUID) -> tuple[int, int]:
    """Счётчики видео — и проверка, что они совпадают с фактическими строками reactions."""
    stmt = select(Video.likes_count, Video.dislikes_count).where(Video.id == video_id)
    stored = tuple((await db.execute(stmt)).one())
    actual = []
    for kind in (ReactionKind.like, ReactionKind.dislike):
        n = await db.scalar(
            select(func.count())
            .select_from(Reaction)
            .where(Reaction.video_id == video_id, Reaction.kind == kind)
        )
        actual.append(n or 0)
    await db.commit()
    assert stored == tuple(actual), f"счётчики {stored}, строки {tuple(actual)}"
    return stored  # type: ignore[return-value]


# --- Поведение ------------------------------------------------------------------------------


async def test_like_is_idempotent_and_exclusive(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def put(kind: str) -> dict:
        r = await client.put(_url(video.id), json={"kind": kind}, headers=user["auth"])
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        return r.json()

    assert await put("like") == {"my_reaction": "like", "likes_count": 1, "dislikes_count": 0}
    assert await put("like") == {"my_reaction": "like", "likes_count": 1, "dislikes_count": 0}
    assert await put("dislike") == {
        "my_reaction": "dislike",
        "likes_count": 0,
        "dislikes_count": 1,
    }
    assert await _counts(db, video.id) == (0, 1)

    # Смена реакции — новое действие: время реакции обновляется («Понравившиеся», день 9).
    times = []
    for kind in ("like", "dislike"):
        await put(kind)
        times.append(
            await db.scalar(
                select(Reaction.created_at).where(
                    Reaction.video_id == video.id, Reaction.user_id == uuid.UUID(user["id"])
                )
            )
        )
        await db.commit()
    assert times[1] > times[0]
    # Лайки не меняют время правки метаданных видео.
    updated = await db.scalar(select(Video.updated_at).where(Video.id == video.id))
    await db.commit()
    assert updated == video.updated_at

    for _ in range(2):  # снятие идемпотентно
        r = await client.delete(_url(video.id), headers=user["auth"])
        assert r.status_code == 200, r.text
        assert r.json() == {"my_reaction": None, "likes_count": 0, "dislikes_count": 0}
    assert await _counts(db, video.id) == (0, 0)


async def test_card_shows_my_reaction(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.put(_url(video.id), json={"kind": "like"}, headers=user["auth"])
    assert r.status_code == 200

    mine = (await client.get(f"/api/videos/{video.id}", headers=user["auth"])).json()
    assert (mine["my_reaction"], mine["likes_count"]) == ("like", 1)
    for headers in ({}, owner["auth"]):
        card = (await client.get(f"/api/videos/{video.id}", headers=headers)).json()
        assert (card["my_reaction"], card["likes_count"]) == (None, 1)


@pytest.mark.parametrize(
    "payload", [{}, {"kind": "love"}, {"kind": None}, {"kind": "like", "x": 1}]
)
async def test_reaction_validation(client: AsyncClient, db: AsyncSession, payload: dict) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.put(_url(video.id), json=payload, headers=owner["auth"])
    assert r.status_code == 422, r.text
    assert await _counts(db, video.id) == (0, 0)


async def test_reaction_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    s = uuid.uuid4().hex[:8]
    await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    r = await client.put(_url(video.id), json={"kind": "like"})
    assert r.status_code == 403
    assert await _counts(db, video.id) == (0, 0)
    r = await client.put(
        _url(video.id), json={"kind": "like"}, headers={"X-CSRF-Token": client.cookies["csrf"]}
    )
    assert r.status_code == 200, r.text


# --- Доступ: can_view, только готовое -------------------------------------------------------


@pytest.mark.parametrize("viewer_kind", [*MANAGE_VIEWERS, "owner"])
async def test_reaction_access_matrix(
    client: AsyncClient, db: AsyncSession, viewer_kind: str
) -> None:
    owner = await api_user(client, db)
    if viewer_kind == "owner":
        videos, _ = await manage_matrix(client, db, owner, "anon")
        viewer: dict | None = owner
        opens = set(MANAGE_STATES)
    else:
        videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
        opens = MANAGE_OPENS.get(viewer_kind, set())

    for name, video in videos.items():
        if viewer is None:
            expected = 401
        elif name not in opens:
            expected = 404  # как несуществующее: факт существования не раскрывается
        elif name in NOT_INTERACTIVE_STATES:
            expected = 409
        else:
            expected = 200
        for method in ("put", "delete"):
            kwargs = {"json": {"kind": "like"}} if method == "put" else {}
            r = await getattr(client, method)(
                _url(video.id), headers=auth_headers(viewer), **kwargs
            )
            assert r.status_code == expected, (name, method, r.text)
            if expected == 200:
                mine = "like" if method == "put" else None
                assert r.json()["my_reaction"] == mine, (name, method)
        assert await _counts(db, video.id) == (0, 0), name  # DELETE снял, отказ — не ставил


async def test_not_found_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    hidden = await client.put(_url(private.id), json={"kind": "like"}, headers=stranger["auth"])
    missing = await client.put(_url(uuid.uuid4()), json={"kind": "like"}, headers=stranger["auth"])
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()


# --- Гонки ----------------------------------------------------------------------------------


async def test_concurrent_change_waits_and_counts_converge(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Лайк того же пользователя держит транзакцию (настоящий сервис, commit придержан) →
    дизлайк ждёт блокировку строки видео и читает прежнюю реакцию уже после commit лайка.
    Без блокировки дизлайк прочитал бы «реакции нет», дождался вставки лайка на ON CONFLICT,
    обновил строку и прибавил дизлайк, не убрав лайк: счётчики (1, 1) при одной строке."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as like_db:
        like_pid = await backend_pid(like_db)
        real_commit = like_db.commit

        async def gated_commit() -> None:
            holding.set()
            await release.wait()
            await real_commit()

        like_db.commit = gated_commit  # type: ignore[method-assign]
        viewer = await like_db.get(User, uuid.UUID(user["id"]))
        like = asyncio.create_task(
            reactions_service.set_reaction(like_db, viewer, video.id, ReactionKind.like)
        )
        await asyncio.wait_for(holding.wait(), timeout=5)

        dislike = asyncio.create_task(
            client.put(_url(video.id), json={"kind": "dislike"}, headers=user["auth"])
        )
        await wait_for_lock_waiters(like_pid)
        release.set()
        assert (await like).likes_count == 1
        r = await dislike

    assert r.status_code == 200, r.text
    assert r.json() == {"my_reaction": "dislike", "likes_count": 0, "dislikes_count": 1}
    assert await _counts(db, video.id) == (0, 1)


async def test_many_concurrent_reactions_keep_counters_exact(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Пачка параллельных лайков, дизлайков и снятий от 8 пользователей: счётчики видео
    в итоге равны числу строк каждого вида."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    users = [await api_user(client, db) for _ in range(8)]
    # Детерминированная смесь действий: у каждого пользователя свой порядок.
    actions = ["like", "dislike", None]

    async def act(user: dict, step: int) -> int:
        choice = actions[step % len(actions)]
        if choice is None:
            r = await client.delete(_url(video.id), headers=user["auth"])
        else:
            r = await client.put(_url(video.id), json={"kind": choice}, headers=user["auth"])
        return r.status_code

    statuses = await asyncio.gather(*(act(u, i + j) for i, u in enumerate(users) for j in range(3)))
    assert set(statuses) == {200}, statuses
    await _counts(db, video.id)


async def _reaction_waits_for(
    client: AsyncClient,
    db: AsyncSession,
    holder_coro_factory,
    reactor: dict,
    video_id: uuid.UUID,
) -> int:
    """Держатель (настоящий сервис в своей сессии, commit придержан) блокирует строку видео;
    реакция ждёт его commit. Возвращает статус ответа реакции."""
    holding, release = asyncio.Event(), asyncio.Event()
    async with get_sessionmaker()() as holder_db:
        holder_pid = await backend_pid(holder_db)
        real_commit = holder_db.commit

        async def gated_commit() -> None:
            holding.set()
            await release.wait()
            await real_commit()

        holder_db.commit = gated_commit  # type: ignore[method-assign]
        holder = asyncio.create_task(holder_coro_factory(holder_db))
        await asyncio.wait_for(holding.wait(), timeout=5)
        reaction = asyncio.create_task(
            client.put(_url(video_id), json={"kind": "like"}, headers=reactor["auth"])
        )
        await wait_for_lock_waiters(holder_pid)
        release.set()
        await holder
        r = await reaction
    return r.status_code


async def test_reaction_rechecks_access_after_lock(client: AsyncClient, db: AsyncSession) -> None:
    """Владелец делает видео приватным (PATCH держит строку), посторонний в это время ставит
    лайк: права проверены до блокировки, но под ней видео уже приватное — 404, лайка нет."""
    owner, stranger = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def make_private(session: AsyncSession):
        me = await session.get(User, uuid.UUID(owner["id"]))
        return await manage_service.update_video(
            session, me, video.id, VideoUpdateIn(visibility=PRIV)
        )

    assert await _reaction_waits_for(client, db, make_private, stranger, video.id) == 404
    assert await _counts(db, video.id) == (0, 0)


async def test_reaction_on_video_deleted_while_waiting(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Удаление держит строку видео — реакция ждёт и после commit удаления получает 404
    (без 500 и без взаимной блокировки)."""
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def delete(session: AsyncSession):
        me = await session.get(User, uuid.UUID(owner["id"]))
        return await manage_service.delete_video(session, me, video.id)

    assert await _reaction_waits_for(client, db, delete, user, video.id) == 404


@pytest.mark.parametrize("role", [None, UserRole.moderator, UserRole.admin])
async def test_hidden_video_rejects_reactions_even_for_owner_and_staff(
    client: AsyncClient, db: AsyncSession, role: UserRole | None
) -> None:
    """Реакции — публичное голосование: на скрытом модератором видео их не ставит никто,
    включая владельца и staff (решение архитектора 2026-11-09). Понятная причина — 409."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB, READY, HIDDEN)
    actor = owner if role is None else await api_user(client, db, role)
    r = await client.put(_url(video.id), json={"kind": "like"}, headers=actor["auth"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == videos_service.HIDDEN_NO_INTERACTION
    assert await _counts(db, video.id) == (0, 0)


async def test_reaction_rejected_if_hidden_while_waiting(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Модератор скрывает видео (строка заблокирована, commit придержан), владелец в это время
    ставит лайк: под блокировкой видео уже скрыто — 409, лайка нет. Сторона модерации здесь —
    прямое изменение строки (эндпоинт модерации появится на дне 6)."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def hide(session: AsyncSession) -> None:
        locked = await videos_repo.lock_video(session, video.id)
        assert locked is not None
        locked.moderation = HIDDEN
        await session.commit()

    assert await _reaction_waits_for(client, db, hide, owner, video.id) == 409
    assert await _counts(db, video.id) == (0, 0)
