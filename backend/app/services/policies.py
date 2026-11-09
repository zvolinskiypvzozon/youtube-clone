"""Единая политика доступа к видео (ADR 0006 и его дополнение).

Любой путь, который отдаёт видео, его файлы, комментарии или включает видео в список,
обязан пройти через этот модуль. Правил три, и они различаются намеренно:

  - `can_view(viewer, facts)` — одно видео: карточка по прямой ссылке, URL потока,
    комментарии, реакции;
  - `visible_filter(viewer)` — SQL-аналог `can_view` для списков «видимое мне»: плейлисты,
    «Доступные мне», история, понравившиеся. Unlisted и приватные с допуском в них есть;
  - `listed_filter()` — каталог: лента, поиск, страница канала. Только готовые публичные
    не скрытые видео, одинаково для любого зрителя (владелец и модераторы видят остальное
    в «Моих видео» и в модерации, а не в каталоге).

Плюс `can_manage(viewer, owner_id)` — правка, допуск, удаление: только владелец, и
`owned_filter(viewer)` — его SQL-аналог для «Моих видео» (все свои видео в любом статусе).

Кто видит одно видео (`can_view`):
  - владелец, модератор, админ — всегда (скрытое и необработанное тоже);
  - остальные — только готовое (`ready`) и не скрытое модератором (`ok`), и при этом
    public/unlisted — всем, включая анонима; private — только из списка допуска.
Отказ вызывающий превращает в 404, а не 403: факт существования не раскрывается.

Заблокированный пользователь сюда не попадает: `auth.authenticate` для него возвращает None,
то есть он — аноним.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, exists, literal_column, or_, select, true

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

STAFF_ROLES = frozenset({UserRole.moderator, UserRole.admin})


@dataclass(frozen=True)
class VideoFacts:
    """Всё, что нужно политике про одно видео и конкретного зрителя."""

    owner_id: uuid.UUID
    visibility: VideoVisibility
    status: VideoStatus
    moderation: ModerationState
    viewer_has_access: bool  # зритель в списке допуска (video_access)

    @classmethod
    def of(cls, video: Video, *, owner_id: uuid.UUID, viewer_has_access: bool) -> "VideoFacts":
        return cls(
            owner_id=owner_id,
            visibility=video.visibility,
            status=video.status,
            moderation=video.moderation,
            viewer_has_access=viewer_has_access,
        )


def is_staff(viewer: User | None) -> bool:
    return viewer is not None and viewer.role in STAFF_ROLES


def can_view(viewer: User | None, facts: VideoFacts) -> bool:
    if viewer is not None and (viewer.id == facts.owner_id or is_staff(viewer)):
        return True
    if facts.status != VideoStatus.ready or facts.moderation != ModerationState.ok:
        return False
    if facts.visibility != VideoVisibility.private:
        return True
    return viewer is not None and facts.viewer_has_access


def can_interact(video: Video) -> bool:
    """Реакции и новые комментарии — публичное взаимодействие: только с готовым и не скрытым
    модератором видео, для любого зрителя, включая владельца и staff (решение архитектора
    2026-11-09, день 5). Доступ staff к скрытому — для разбора, а не для голосования.
    Проверяется после can_view: кто не может видеть видео, получает 404 раньше."""
    return video.status == VideoStatus.ready and video.moderation == ModerationState.ok


def sees_internal_state(viewer: User | None, owner_id: uuid.UUID) -> bool:
    """Статус обработки, видимость, пометка модерации и причина отказа: владельцу и staff.
    Остальные получают только публичную карточку."""
    return viewer is not None and (viewer.id == owner_id or is_staff(viewer))


def can_manage(viewer: User | None, owner_id: uuid.UUID) -> bool:
    """Правка метаданных и видимости, список допуска, удаление. Модерация (день 6) —
    отдельные права, не через эту функцию."""
    return viewer is not None and viewer.id == owner_id


# --- SQL ------------------------------------------------------------------------------------
# Константы — SQL-литералы, а не bind-параметры: условие каталога должно совпасть
# с предикатом частичного индекса ix_videos_feed. С параметрами ($1) generic-план
# prepared statement (asyncpg) не может доказать предикат и индекс не использует.
# Литералы зафиксированы здесь, без функции с параметром: ввод в SQL попасть не может.
_READY = literal_column(f"'{VideoStatus.ready.value}'")
_PUBLIC = literal_column(f"'{VideoVisibility.public.value}'")
_PRIVATE = literal_column(f"'{VideoVisibility.private.value}'")
_MOD_OK = literal_column(f"'{ModerationState.ok.value}'")


def listed_filter() -> ColumnElement[bool]:
    """Каталог: лента, поиск, канал. Совпадает с предикатом ix_videos_feed."""
    return and_(
        Video.status == _READY,
        Video.visibility == _PUBLIC,
        Video.moderation == _MOD_OK,
    )


def visible_filter(viewer: User | None) -> ColumnElement[bool]:
    """SQL-аналог can_view: условие на строку `videos` для списков «видимое мне».

    viewer — ТЕКУЩИЙ зритель запроса, а не владелец списка. Для чужого плейлиста передавать
    владельца плейлиста нельзя: зритель увидел бы приватные видео, допущенные владельцу."""
    if is_staff(viewer):
        return true()
    available = and_(
        Video.status == _READY,
        Video.moderation == _MOD_OK,
    )
    not_private = Video.visibility != _PRIVATE
    if viewer is None:
        return and_(available, not_private)
    owned = Video.channel_id.in_(select(Channel.id).where(Channel.owner_id == viewer.id))
    granted = exists().where(VideoAccess.video_id == Video.id, VideoAccess.user_id == viewer.id)
    return or_(owned, and_(available, or_(not_private, granted)))


def owned_filter(viewer: User) -> ColumnElement[bool]:
    """«Мои видео»: видео канала зрителя в любом статусе, видимости и модерации — SQL-аналог
    can_manage. Только свой канал и для staff: чужие видео модераторы видят в модерации.

    Равенство со скалярным подзапросом (канал у пользователя один): planner вычисляет его
    один раз (InitPlan) и читает ix_videos_channel_created в порядке выдачи."""
    own_channel = select(Channel.id).where(Channel.owner_id == viewer.id).scalar_subquery()
    return Video.channel_id == own_channel
