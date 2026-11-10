"""Комментарии: GET/POST /api/videos/{id}/comments, DELETE /api/comments/{id}.

Читать — can_view (иначе 404); писать — can_view, затем can_interact (скрытое и не готовое —
409 для всех, включая владельца и staff); удалять — автор или владелец видео (ADR 0006,
доп. 5): комментарий не виден → 404, виден, но прав нет → 403. Счётчик comments_count
меняется в той же транзакции; условия политики при записи проверяются под блокировкой строки.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Comment, User, UserRole, Video, VideoAccess
from app.repositories import videos as videos_repo
from app.schemas.videos import VideoUpdateIn
from app.services import comments as comments_service
from app.services import manage as manage_service
from app.services import videos as videos_service
from tests.helpers import (
    HIDDEN,
    MANAGE_OPENS,
    MANAGE_STATES,
    MANAGE_VIEWERS,
    PRIV,
    PUB,
    api_user,
    auth_headers,
    channel_of,
    manage_matrix,
    seed_video,
    waits_for,
)

NOT_READY_STATES = {"processing", "failed"}


def _url(video_id: uuid.UUID) -> str:
    return f"/api/videos/{video_id}/comments"


def _del_url(comment_id: str | uuid.UUID) -> str:
    return f"/api/comments/{comment_id}"


async def _count(db: AsyncSession, video_id: uuid.UUID) -> tuple[int, int]:
    """(videos.comments_count, фактическое число строк comments)."""
    counter = await db.scalar(select(Video.comments_count).where(Video.id == video_id))
    rows = await db.scalar(select(func.count()).where(Comment.video_id == video_id))
    await db.commit()
    return counter or 0, rows or 0


async def _post(client: AsyncClient, user: dict, video_id: uuid.UUID, body: str = "Привет") -> str:
    r = await client.post(_url(video_id), json={"body": body}, headers=user["auth"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- Создание и чтение -----------------------------------------------------------------------


async def test_create_and_list(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    r = await client.post(_url(video.id), json={"body": "  Отлично!\n"}, headers=user["auth"])
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["body"] == "Отлично!"  # пробелы по краям обрезаются
    assert created["author"]["handle"] == user["username"]
    assert created["can_delete"] is True

    page = (await client.get(_url(video.id))).json()
    assert [c["id"] for c in page["items"]] == [created["id"]]
    assert page["items"][0]["can_delete"] is False  # аноним
    assert page["next_cursor"] is None
    assert await _count(db, video.id) == (1, 1)
    assert (await client.get(f"/api/videos/{video.id}")).json()["comments_count"] == 1
    # Комментарий не меняет время правки метаданных видео.
    updated = await db.scalar(select(Video.updated_at).where(Video.id == video.id))
    await db.commit()
    assert updated == video.updated_at


async def test_html_is_stored_as_plain_text(client: AsyncClient, db: AsyncSession) -> None:
    """Сервер не экранирует и не чистит HTML: текст хранится и отдаётся как есть, вывод —
    только как текст (фронтенд, без dangerouslySetInnerHTML; security.md)."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    body = '<img src=x onerror="alert(1)"> & <b>'
    await _post(client, owner, video.id, body)
    assert (await client.get(_url(video.id))).json()["items"][0]["body"] == body


@pytest.mark.parametrize("viewer_kind", [*MANAGE_VIEWERS, "owner"])
async def test_read_access_matrix(client: AsyncClient, db: AsyncSession, viewer_kind: str) -> None:
    """Читать — по can_view: под скрытым видео владелец и staff видят комментарии (разбор)."""
    owner = await api_user(client, db)
    if viewer_kind == "owner":
        videos, _ = await manage_matrix(client, db, owner, "anon")
        viewer: dict | None = owner
        opens = set(MANAGE_STATES)
    else:
        videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
        opens = MANAGE_OPENS.get(viewer_kind, {"public", "unlisted"})
    for video in videos.values():
        db.add(Comment(video_id=video.id, author_id=uuid.UUID(owner["id"]), body="секрет"))
    await db.commit()

    for name, video in videos.items():
        r = await client.get(_url(video.id), headers=auth_headers(viewer))
        if name in opens:
            assert r.status_code == 200, (name, r.text)
            assert [c["body"] for c in r.json()["items"]] == ["секрет"], name
        else:
            assert r.status_code == 404, (name, r.text)
            assert "секрет" not in r.text


@pytest.mark.parametrize("viewer_kind", [*MANAGE_VIEWERS, "owner"])
async def test_write_access_matrix(client: AsyncClient, db: AsyncSession, viewer_kind: str) -> None:
    """Писать: не может открыть — 404; не готово или скрыто — 409 для всех, включая
    владельца и staff; аноним — 401."""
    owner = await api_user(client, db)
    if viewer_kind == "owner":
        videos, _ = await manage_matrix(client, db, owner, "anon")
        viewer: dict | None = owner
        opens = set(MANAGE_STATES)
    else:
        videos, viewer = await manage_matrix(client, db, owner, viewer_kind)
        opens = MANAGE_OPENS.get(viewer_kind, {"public", "unlisted"})

    for name, video in videos.items():
        r = await client.post(_url(video.id), json={"body": "x"}, headers=auth_headers(viewer))
        if viewer is None:
            expected = 401
        elif name not in opens:
            expected = 404
        elif name in NOT_READY_STATES or name == "hidden":
            expected = 409
        else:
            expected = 201
        assert r.status_code == expected, (name, r.text)
        if name == "hidden" and expected == 409:
            assert r.json()["detail"] == videos_service.HIDDEN_NO_INTERACTION
        assert await _count(db, video.id) == ((1, 1) if expected == 201 else (0, 0)), name


async def test_not_found_identical_for_missing_and_private(
    client: AsyncClient, db: AsyncSession
) -> None:
    owner, stranger = await api_user(client, db), await api_user(client, db)
    private = await seed_video(db, await channel_of(db, owner), PRIV)
    for method in ("get", "post"):
        kwargs = {"headers": stranger["auth"]}
        if method == "post":
            kwargs["json"] = {"body": "x"}
        hidden = await getattr(client, method)(_url(private.id), **kwargs)
        missing = await getattr(client, method)(_url(uuid.uuid4()), **kwargs)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()


@pytest.mark.parametrize(
    "body",
    ["", "   ", "\n\t", "a" * 2001, "привет\x00", "\x00"],
    ids=["empty", "spaces", "whitespace", "too-long", "nul-inside", "nul-only"],
)
async def test_invalid_body_is_422(client: AsyncClient, db: AsyncSession, body: str) -> None:
    """NUL Postgres в text не принимает: без проверки в схеме был бы 500 (как в аудите 3b)."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    r = await client.post(_url(video.id), json={"body": body}, headers=owner["auth"])
    assert r.status_code == 422, r.text
    assert await _count(db, video.id) == (0, 0)


async def test_body_limits(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _post(client, owner, video.id, "я" * 2000)
    await _post(client, owner, video.id, "  " + "я" * 2000 + "  ")  # длина — после обрезки
    r = await client.post(
        _url(video.id), json={"body": "x", "author_id": owner["id"]}, headers=owner["auth"]
    )
    assert r.status_code == 422  # лишние поля запрещены
    assert await _count(db, video.id) == (2, 2)


async def test_pagination_newest_first(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    ids = [await _post(client, owner, video.id, f"c{i}") for i in range(5)]

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get(_url(video.id), params=params)).json()
        seen += [c["id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids[::-1]

    r = await client.get(_url(video.id), params={"cursor": "мусор"})
    assert r.status_code == 400


async def test_comments_of_other_video_not_listed(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    channel = await channel_of(db, owner)
    a, b = await seed_video(db, channel, PUB), await seed_video(db, channel, PUB)
    await _post(client, owner, a.id, "к видео A")
    assert (await client.get(_url(b.id))).json()["items"] == []


async def test_cascade_on_video_delete(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _post(client, owner, video.id)
    r = await client.delete(f"/api/videos/{video.id}", headers=owner["auth"])
    assert r.status_code == 204, r.text
    assert await _count(db, video.id) == (0, 0)


# --- Удаление --------------------------------------------------------------------------------


async def test_can_delete_flags(client: AsyncClient, db: AsyncSession) -> None:
    owner, author, other = [await api_user(client, db) for _ in range(3)]
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _post(client, author, video.id, "автор")
    await _post(client, other, video.id, "другой")

    def flags(page: Response) -> dict[str, bool]:
        return {c["body"]: c["can_delete"] for c in page.json()["items"]}

    assert flags(await client.get(_url(video.id), headers=owner["auth"])) == {
        "автор": True,
        "другой": True,
    }
    assert flags(await client.get(_url(video.id), headers=author["auth"])) == {
        "автор": True,
        "другой": False,
    }
    moderator = await api_user(client, db, UserRole.moderator)
    assert flags(await client.get(_url(video.id), headers=moderator["auth"])) == {
        "автор": True,  # staff удаляет любой (модерация, с записью в журнал)
        "другой": True,
    }


async def test_author_and_video_owner_delete(client: AsyncClient, db: AsyncSession) -> None:
    owner, author = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    own = await _post(client, author, video.id)
    foreign = await _post(client, author, video.id)

    assert (await client.delete(_del_url(own), headers=author["auth"])).status_code == 204
    assert (await client.delete(_del_url(foreign), headers=owner["auth"])).status_code == 204
    assert await _count(db, video.id) == (0, 0)
    # Повтор — 404: комментария больше нет, счётчик не уходит в минус.
    assert (await client.delete(_del_url(own), headers=author["auth"])).status_code == 404
    assert await _count(db, video.id) == (0, 0)


async def test_visible_but_not_allowed_is_403(client: AsyncClient, db: AsyncSession) -> None:
    """Посторонний user видит комментарий, но не автор и не владелец видео — 403. Staff
    удаляет любой (test_moderation)."""
    owner, author = await api_user(client, db), await api_user(client, db)
    actor = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    comment = await _post(client, author, video.id)
    r = await client.delete(_del_url(comment), headers=actor["auth"])
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == comments_service.NOT_COMMENT_OWNER
    assert await _count(db, video.id) == (1, 1)


async def test_invisible_comment_is_404_like_missing(client: AsyncClient, db: AsyncSession) -> None:
    """Комментарий под приватным видео (автор — с допуском) для постороннего неотличим
    от несуществующего: тот же 404 и тот же текст."""
    owner, author, stranger = [await api_user(client, db) for _ in range(3)]
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    db.add(VideoAccess(video_id=video.id, user_id=uuid.UUID(author["id"])))
    await db.commit()
    comment = await _post(client, author, video.id)

    hidden = await client.delete(_del_url(comment), headers=stranger["auth"])
    missing = await client.delete(_del_url(uuid.uuid4()), headers=stranger["auth"])
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert await _count(db, video.id) == (1, 1)


async def test_delete_requires_login(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    comment = await _post(client, owner, video.id)
    assert (await client.delete(_del_url(comment))).status_code == 401
    assert await _count(db, video.id) == (1, 1)


async def test_author_deletes_after_losing_access(client: AsyncClient, db: AsyncSession) -> None:
    """Автор удаляет свой комментарий, даже потеряв допуск к приватному видео: это его данные."""
    owner, author = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PRIV)
    db.add(VideoAccess(video_id=video.id, user_id=uuid.UUID(author["id"])))
    await db.commit()
    comment = await _post(client, author, video.id)
    await db.execute(delete(VideoAccess).where(VideoAccess.video_id == video.id))
    await db.commit()
    assert (await client.get(_url(video.id), headers=author["auth"])).status_code == 404

    assert (await client.delete(_del_url(comment), headers=author["auth"])).status_code == 204
    assert await _count(db, video.id) == (0, 0)


@pytest.mark.parametrize("who", ["owner", "author"])
async def test_delete_under_hidden_video(client: AsyncClient, db: AsyncSession, who: str) -> None:
    """Удаление — не взаимодействие: под скрытым видео разрешено (ADR 0006 доп. 5)."""
    owner, author = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    comment = await _post(client, author, video.id)
    await db.execute(
        update(Video)
        .where(Video.id == video.id)
        .values(moderation=HIDDEN, moderation_reason="нарушение")
    )
    await db.commit()
    actor = owner if who == "owner" else author
    assert (await client.delete(_del_url(comment), headers=actor["auth"])).status_code == 204
    assert await _count(db, video.id) == (0, 0)


async def test_delete_via_cookie_requires_csrf(client: AsyncClient, db: AsyncSession) -> None:
    s = uuid.uuid4().hex[:8]
    r = await client.post(
        "/api/auth/register",
        json={"email": f"c_{s}@example.com", "username": f"c_{s}", "password": "pass-pass-1"},
    )
    me = {"id": r.json()["id"]}
    video = await seed_video(db, await channel_of(db, me), PUB)
    csrf = {"X-CSRF-Token": client.cookies["csrf"]}
    assert (await client.post(_url(video.id), json={"body": "x"})).status_code == 403
    r = await client.post(_url(video.id), json={"body": "x"}, headers=csrf)
    assert r.status_code == 201, r.text
    assert (await client.delete(_del_url(r.json()["id"]))).status_code == 403
    assert (await client.delete(_del_url(r.json()["id"]), headers=csrf)).status_code == 204


# --- Гонки -----------------------------------------------------------------------------------


async def test_concurrent_posts_are_not_lost(client: AsyncClient, db: AsyncSession) -> None:
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    rs = await asyncio.gather(
        *(
            client.post(_url(video.id), json={"body": f"{i}"}, headers=owner["auth"])
            for i in range(20)
        )
    )
    assert {r.status_code for r in rs} == {201}
    assert await _count(db, video.id) == (20, 20)


async def test_post_rejected_if_made_private_while_waiting(
    client: AsyncClient, db: AsyncSession
) -> None:
    """Владелец делает видео приватным (PATCH держит строку), посторонний в это время пишет:
    права проверены до записи, но UPDATE счётчика ждёт блокировку и проверяет visible_filter
    по новой версии строки — 404, комментария нет."""
    owner, stranger = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def make_private(session: AsyncSession) -> object:
        me = await session.get(User, uuid.UUID(owner["id"]))
        return await manage_service.update_video(
            session, me, video.id, VideoUpdateIn(visibility=PRIV)
        )

    [r] = await waits_for(
        make_private,
        [lambda: client.post(_url(video.id), json={"body": "x"}, headers=stranger["auth"])],
    )
    assert r.status_code == 404, r.text
    assert await _count(db, video.id) == (0, 0)


async def test_post_rejected_if_hidden_while_waiting(client: AsyncClient, db: AsyncSession) -> None:
    """Модератор скрывает видео (прямое изменение строки — эндпоинт модерации на дне 6),
    владелец в это время пишет: под блокировкой видео уже скрыто — 409."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def hide(session: AsyncSession) -> None:
        locked = await videos_repo.lock_video(session, video.id)
        assert locked is not None
        locked.moderation = HIDDEN
        locked.moderation_reason = "нарушение"  # CHECK: причина ⇔ скрыто
        await session.commit()

    [r] = await waits_for(
        hide, [lambda: client.post(_url(video.id), json={"body": "x"}, headers=owner["auth"])]
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == videos_service.HIDDEN_NO_INTERACTION
    assert await _count(db, video.id) == (0, 0)


async def test_post_on_video_deleted_while_waiting(client: AsyncClient, db: AsyncSession) -> None:
    owner, user = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def delete_video(session: AsyncSession) -> None:
        me = await session.get(User, uuid.UUID(owner["id"]))
        await manage_service.delete_video(session, me, video.id)

    [r] = await waits_for(
        delete_video,
        [lambda: client.post(_url(video.id), json={"body": "x"}, headers=user["auth"])],
    )
    assert r.status_code == 404, r.text
    assert await _count(db, video.id) == (0, 0)


async def test_concurrent_deletes_decrement_once(client: AsyncClient, db: AsyncSession) -> None:
    """Автор и владелец видео одновременно удаляют один комментарий: оба прошли проверку прав
    и ждут строку видео; удаляет первый, второй получает 404, счётчик уменьшается один раз."""
    owner, author = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    await _post(client, owner, video.id, "остаётся")
    comment = await _post(client, author, video.id)

    async def hold(session: AsyncSession) -> None:
        assert await videos_repo.lock_for_edit(session, video.id) is not None
        await session.commit()

    rs = await waits_for(
        hold,
        [
            lambda: client.delete(_del_url(comment), headers=author["auth"]),
            lambda: client.delete(_del_url(comment), headers=owner["auth"]),
        ],
    )
    assert sorted(r.status_code for r in rs) == [204, 404]
    assert await _count(db, video.id) == (1, 1)


async def test_delete_comment_while_video_deleted(client: AsyncClient, db: AsyncSession) -> None:
    """Удаление видео держит его строку (и каскадом — строки комментариев); удаление
    комментария ждёт строку видео (порядок «видео → комментарий») и получает 404."""
    owner, author = await api_user(client, db), await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)
    comment = await _post(client, author, video.id)

    async def delete_video(session: AsyncSession) -> None:
        me = await session.get(User, uuid.UUID(owner["id"]))
        await manage_service.delete_video(session, me, video.id)

    [r] = await waits_for(
        delete_video, [lambda: client.delete(_del_url(comment), headers=author["auth"])]
    )
    assert r.status_code == 404, r.text
    assert await _count(db, video.id) == (0, 0)


async def test_post_busy_if_state_flipped_back(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UPDATE счётчика не подошёл (видео на миг стало недоступным), а перепроверка видит
    его снова доступным: 503 с Retry-After, комментария нет. Окно не воспроизводится
    детерминированно — UPDATE подменён."""
    owner = await api_user(client, db)
    video = await seed_video(db, await channel_of(db, owner), PUB)

    async def no_row(*_args: object) -> bool:
        return False

    monkeypatch.setattr(comments_service.videos_repo, "add_comment_count", no_row)
    r = await client.post(_url(video.id), json={"body": "x"}, headers=owner["auth"])
    assert r.status_code == 503, r.text
    assert r.headers.get("Retry-After")
    assert await _count(db, video.id) == (0, 0)
