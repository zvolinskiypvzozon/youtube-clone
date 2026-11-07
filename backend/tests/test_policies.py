"""Политика доступа к видео (ADR 0006): матрица «зритель × состояние видео».

Ожидаемые значения заданы явными множествами ниже, а не повторным вычислением логики.
Проверяются три пути, которые обязаны давать одинаковый ответ:
  - чистая функция can_view;
  - can_view на фактах, загруженных из БД (repositories.videos.get_for_view);
  - SQL-условие visible_filter.
"""

import itertools
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Channel,
    ModerationState,
    User,
    UserRole,
    Video,
    VideoAccess,
    VideoStatus,
    VideoVisibility,
)
from app.repositories import videos as videos_repo
from app.services import policies
from app.services.policies import VideoFacts
from tests.factories import create_user, create_video

PUB, UNL, PRIV = VideoVisibility.public, VideoVisibility.unlisted, VideoVisibility.private
UPL, PROC, READY, FAIL = (
    VideoStatus.uploading,
    VideoStatus.processing,
    VideoStatus.ready,
    VideoStatus.failed,
)
OK, HIDDEN = ModerationState.ok, ModerationState.hidden

Combo = tuple[VideoVisibility, VideoStatus, ModerationState]
ALL: frozenset[Combo] = frozenset(itertools.product(VideoVisibility, VideoStatus, ModerationState))
assert len(ALL) == 24

# Чужим (аноним, посторонний): готовые не скрытые, кроме приватных.
OUTSIDER: frozenset[Combo] = frozenset({(PUB, READY, OK), (UNL, READY, OK)})
# Из списка допуска: плюс приватное готовое не скрытое. Скрытое — нет.
GRANTED: frozenset[Combo] = OUTSIDER | {(PRIV, READY, OK)}
# Каталог (лента, поиск, канал) — одинаково для всех зрителей.
LISTED: frozenset[Combo] = frozenset({(PUB, READY, OK)})

VIEWERS = ("anon", "stranger", "granted", "owner", "moderator", "admin")

# Видео канала владельца; у "granted" допуск ко ВСЕМ видео этого канала.
EXPECTED_OWNER_CHANNEL: dict[str, frozenset[Combo]] = {
    "anon": OUTSIDER,
    "stranger": OUTSIDER,
    "granted": GRANTED,
    "owner": ALL,
    "moderator": ALL,
    "admin": ALL,
}
# Видео канала постороннего: владелец первого канала здесь — чужой.
EXPECTED_STRANGER_CHANNEL: dict[str, frozenset[Combo]] = {
    "anon": OUTSIDER,
    "stranger": ALL,
    "granted": OUTSIDER,
    "owner": OUTSIDER,
    "moderator": ALL,
    "admin": ALL,
}


# --- Чистая функция -------------------------------------------------------------------------


def _user(role: UserRole = UserRole.user) -> User:
    return User(id=uuid.uuid4(), role=role)


@pytest.mark.parametrize("viewer_kind", VIEWERS)
@pytest.mark.parametrize("combo", sorted(ALL), ids=lambda c: "-".join(map(str, c)))
def test_can_view_matrix(viewer_kind: str, combo: Combo) -> None:
    owner = _user()
    viewers: dict[str, User | None] = {
        "anon": None,
        "stranger": _user(),
        "granted": _user(),
        "owner": owner,
        "moderator": _user(UserRole.moderator),
        "admin": _user(UserRole.admin),
    }
    visibility, status, moderation = combo
    facts = VideoFacts(
        owner_id=owner.id,
        visibility=visibility,
        status=status,
        moderation=moderation,
        viewer_has_access=viewer_kind == "granted",
    )
    expected = combo in EXPECTED_OWNER_CHANNEL[viewer_kind]
    assert policies.can_view(viewers[viewer_kind], facts) is expected


def test_anonymous_never_sees_private_even_with_access_flag() -> None:
    facts = VideoFacts(uuid.uuid4(), PRIV, READY, OK, viewer_has_access=True)
    assert policies.can_view(None, facts) is False


def test_can_manage_only_owner() -> None:
    owner = _user()
    assert policies.can_manage(owner, owner.id) is True
    assert policies.can_manage(None, owner.id) is False
    assert policies.can_manage(_user(), owner.id) is False
    # Модерация — отдельные права (день 6), не правка чужого видео.
    assert policies.can_manage(_user(UserRole.moderator), owner.id) is False
    assert policies.can_manage(_user(UserRole.admin), owner.id) is False


def test_listed_filter_has_no_bind_params() -> None:
    """Литералы, а не $N: иначе generic-план не докажет предикат частичного ix_videos_feed."""
    compiled = (
        select(Video.id).where(policies.listed_filter()).compile(dialect=postgresql.dialect())
    )
    assert compiled.params == {}
    sql = str(compiled)
    for fragment in (
        "videos.status = 'ready'",
        "videos.visibility = 'public'",
        "videos.moderation = 'ok'",
    ):
        assert fragment in sql


# --- Через БД -------------------------------------------------------------------------------


@dataclass
class World:
    viewers: dict[str, User | None]
    # id видео → (канал: "owner" | "stranger", комбинация)
    videos: dict[uuid.UUID, tuple[str, Combo]]

    def expected(self, viewer_kind: str) -> set[uuid.UUID]:
        tables = {"owner": EXPECTED_OWNER_CHANNEL, "stranger": EXPECTED_STRANGER_CHANNEL}
        return {
            vid
            for vid, (channel, combo) in self.videos.items()
            if combo in tables[channel][viewer_kind]
        }


async def _channel(db: AsyncSession, user: User) -> Channel:
    return (await db.execute(select(Channel).where(Channel.owner_id == user.id))).scalar_one()


async def _all_combos(db: AsyncSession, channel: Channel) -> dict[uuid.UUID, Combo]:
    result = {}
    for visibility, status, moderation in sorted(ALL):
        video = await create_video(db, channel, visibility=visibility, status=status)
        video.moderation = moderation
        result[video.id] = (visibility, status, moderation)
    await db.flush()
    return result


@pytest.fixture
async def world(db: AsyncSession) -> World:
    owner = await create_user(db)
    stranger = await create_user(db)
    granted = await create_user(db)
    moderator = await create_user(db, role=UserRole.moderator)
    admin = await create_user(db, role=UserRole.admin)

    videos: dict[uuid.UUID, tuple[str, Combo]] = {}
    own = await _all_combos(db, await _channel(db, owner))
    videos |= {vid: ("owner", c) for vid, c in own.items()}
    foreign = await _all_combos(db, await _channel(db, stranger))
    videos |= {vid: ("stranger", c) for vid, c in foreign.items()}

    # Допуск только к видео владельца — к видео постороннего "granted" не допущен.
    db.add_all(VideoAccess(video_id=vid, user_id=granted.id) for vid in own)
    await db.commit()
    # expire_on_commit=False: без этого get_for_view вернул бы объекты из identity map
    # с атрибутами фикстуры, а не прочитанные из БД.
    db.expunge_all()
    return World(
        viewers={
            "anon": None,
            "stranger": stranger,
            "granted": granted,
            "owner": owner,
            "moderator": moderator,
            "admin": admin,
        },
        videos=videos,
    )


@pytest.mark.parametrize("viewer_kind", VIEWERS)
async def test_visible_filter_matrix(db: AsyncSession, world: World, viewer_kind: str) -> None:
    viewer = world.viewers[viewer_kind]
    found = set(await db.scalars(select(Video.id).where(policies.visible_filter(viewer))))
    assert found == world.expected(viewer_kind)


@pytest.mark.parametrize("viewer_kind", VIEWERS)
async def test_can_view_on_loaded_facts_matrix(
    db: AsyncSession, world: World, viewer_kind: str
) -> None:
    viewer = world.viewers[viewer_kind]
    allowed = set()
    for vid in world.videos:
        loaded = await videos_repo.get_for_view(db, vid, viewer.id if viewer else None)
        assert loaded is not None
        video, channel, has_access = loaded
        facts = VideoFacts.of(video, owner_id=channel.owner_id, viewer_has_access=has_access)
        if policies.can_view(viewer, facts):
            allowed.add(vid)
    assert allowed == world.expected(viewer_kind)


async def test_listed_filter_is_catalog_only(db: AsyncSession, world: World) -> None:
    # От зрителя не зависит по сигнатуре: каталог одинаков для всех, включая владельца и staff.
    found = set(await db.scalars(select(Video.id).where(policies.listed_filter())))
    assert found == {vid for vid, (_, combo) in world.videos.items() if combo in LISTED}
    assert len(found) == 2  # по одному на каждый канал


async def test_get_for_view_access_flag_is_per_viewer(db: AsyncSession, world: World) -> None:
    vid = next(v for v, (ch, c) in world.videos.items() if ch == "owner" and c == (PRIV, READY, OK))
    granted, stranger = world.viewers["granted"], world.viewers["stranger"]
    assert granted is not None and stranger is not None
    assert (await videos_repo.get_for_view(db, vid, granted.id))[2] is True
    assert (await videos_repo.get_for_view(db, vid, stranger.id))[2] is False
    assert (await videos_repo.get_for_view(db, vid, None))[2] is False


async def test_get_for_view_missing_video(db: AsyncSession) -> None:
    assert await videos_repo.get_for_view(db, uuid.uuid4(), None) is None
