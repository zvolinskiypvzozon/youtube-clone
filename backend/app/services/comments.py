"""Комментарии: плоские, без правки (упрощение дня 5).

Права — только через policies (ADR 0006, доп. 5):
  - читать — `can_view` (иначе 404), в том числе под скрытым видео (владелец и staff
    разбирают его вместе с комментариями);
  - писать — `can_view` (404), затем `can_interact` (409): под скрытым и не готовым видео
    не пишет никто, включая владельца и staff;
  - удалять — `can_delete_comment`: автор, владелец видео или staff (модерация: запись
    в moderation_log со снимком текста). Не виден комментарий → 404, виден, но прав нет → 403.

Счётчик `videos.comments_count`:
  - добавление — атомарный UPDATE с условиями политики в самом запросе (visible_filter +
    interactable_filter, как у просмотров), затем INSERT в той же транзакции. UPDATE держит
    блокировку строки видео до commit, поэтому права проверяются по актуальной версии строки
    (PATCH «сделать приватным» или скрытие модератором, пришедшие раньше, учтены);
  - удаление — порядок блокировок «видео → комментарий»: сначала строка видео
    (lock_for_edit), затем DELETE … RETURNING, и только если строку удалил этот запрос —
    счётчик −1. Обратный порядок взаимно блокировался бы с удалением видео (оно держит видео
    и каскадом ждёт строки комментариев).
"""

import uuid

from sqlalchemy import and_, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Busy, Forbidden, NotFound
from app.models import Channel, Comment, ModerationAction, User
from app.repositories import channels as channels_repo
from app.repositories import comments as comments_repo
from app.repositories import moderation as moderation_repo
from app.repositories import videos as videos_repo
from app.schemas.videos import ChannelBrief, CommentOut, CommentPage
from app.services import bans, cursor, policies
from app.services import videos as videos_service

COMMENT_NOT_FOUND = "Комментарий не найден"
NOT_COMMENT_OWNER = "Удалить комментарий может только его автор, владелец видео или модератор"
VIDEO_CHANGING = "Видео сейчас изменяется. Повторите попытку через несколько секунд"


def _out(
    comment: Comment,
    author: Channel,
    can_delete: bool,
    *,
    staff: bool = False,
    author_banned: bool = False,
) -> CommentOut:
    """Автор как пользователь и признак бана — только staff (для бана из модерации);
    остальным null (решение архитектора 2026-11-10)."""
    return CommentOut(
        id=comment.id,
        body=comment.body,
        created_at=comment.created_at,
        author=ChannelBrief(id=author.id, handle=author.handle, title=author.title),
        can_delete=can_delete,
        author_user_id=comment.author_id if staff else None,
        author_banned=author_banned if staff else None,
    )


async def list_comments(
    db: AsyncSession, viewer: User | None, video_id: uuid.UUID, after_cursor: str | None, limit: int
) -> CommentPage:
    after = cursor.decode(after_cursor) if after_cursor else None
    viewable = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    owner_id = viewable.channel.owner_id
    rows = await comments_repo.page(
        db,
        video_id,
        lambda author_banned: policies.comment_visible_filter(viewer, author_banned),
        after,
        limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    staff = policies.is_staff(viewer)
    items = [
        _out(
            r.comment,
            r.author,
            policies.can_delete_comment(viewer, r.comment.author_id, owner_id),
            staff=staff,
            author_banned=r.author_banned,
        )
        for r in rows
    ]
    last = rows[-1].comment if rows else None
    next_cursor = cursor.encode(last.created_at, last.id) if has_more and last else None
    return CommentPage(items=items, next_cursor=next_cursor)


async def add_comment(db: AsyncSession, viewer: User, video_id: uuid.UUID, body: str) -> CommentOut:
    viewable = await videos_service.get_viewable(db, viewer, video_id)  # can_view, иначе 404
    # готово, не скрыто, автор видео не забанен — иначе 409
    videos_service.ensure_interactive(viewable.video, owner_banned=viewable.owner_banned)
    await bans.lock_self_active(db, viewer)  # «пользователь → видео», как у бана (доп. 8)
    where = and_(policies.visible_filter(viewer), policies.interactable_filter())
    if not await videos_repo.add_comment_count(db, video_id, +1, where):
        # Пока ждали блокировку, видео удалили, сделали недоступным или скрыли: причина —
        # по свежему состоянию (404 или 409). Если оно уже вернулось обратно — повторить.
        # Не rollback: он просрочил бы и viewer (обращение к атрибутам — MissingGreenlet).
        # Новый SELECT в READ COMMITTED видит закоммиченное; expire — чтобы строка видео
        # перечиталась, а не взялась из identity map.
        db.expire(viewable.video)
        viewable = await videos_service.get_viewable(db, viewer, video_id)
        videos_service.ensure_interactive(viewable.video, owner_banned=viewable.owner_banned)
        raise Busy(VIDEO_CHANGING)
    comment = await comments_repo.create(db, video_id, viewer.id, body)
    author = await channels_repo.get_by_owner(db, viewer.id)
    out = _out(comment, author, can_delete=True)  # автор всегда может удалить своё
    await db.commit()
    return out


async def delete_comment(db: AsyncSession, viewer: User, comment_id: uuid.UUID) -> None:
    found = await comments_repo.get_with_video_owner(db, comment_id)
    if found is None:
        raise NotFound(COMMENT_NOT_FOUND)
    comment, video_owner_id = found
    video_id = comment.video_id
    # Комментарий забаненного не-staff не видит — и удаление ведёт себя как с
    # несуществующим (404), а не 403/204: иначе ответ раскрыл бы скрытый комментарий
    # (аудит шага 3). Владелец видео удалит его после разбана.
    if not policies.is_staff(viewer) and await comments_repo.author_banned(db, comment.author_id):
        raise NotFound(COMMENT_NOT_FOUND)
    if not policies.can_delete_comment(viewer, comment.author_id, video_owner_id):
        # Комментарий виден (видео видно) → 403; не виден → 404 с тем же текстом, что
        # у несуществующего: ответ не раскрывает существование комментария и видео.
        loaded = await videos_repo.get_for_view(db, video_id, viewer.id)
        if loaded is not None:
            facts = policies.VideoFacts.of(
                loaded.video,
                owner_id=loaded.channel.owner_id,
                viewer_has_access=loaded.has_access,
                owner_banned=loaded.owner_banned,
            )
            if policies.can_view(viewer, facts):
                raise Forbidden(NOT_COMMENT_OWNER)
        raise NotFound(COMMENT_NOT_FOUND)

    # Права не зависят от изменяемого состояния видео (автор и владелец не меняются),
    # поэтому под блокировкой не перепроверяются.
    if await videos_repo.lock_for_edit(db, video_id) is None:  # видео удалили, пока ждали
        await db.rollback()
        raise NotFound(COMMENT_NOT_FOUND)
    if not await comments_repo.remove(db, comment_id):  # параллельное удаление успело раньше
        await db.rollback()
        raise NotFound(COMMENT_NOT_FOUND)
    # Комментарий забаненного в счётчике не учтён (бан пересчитал его под этой же
    # блокировкой видео) — уменьшать нечего.
    if not await comments_repo.author_banned(db, comment.author_id):
        await videos_repo.add_comment_count(db, video_id, -1, true())
    # Удалил staff не своё и не под своим видео — модерация, запись в журнал.
    if policies.is_staff(viewer) and viewer.id not in (comment.author_id, video_owner_id):
        await moderation_repo.log(
            db,
            actor_id=viewer.id,
            action=ModerationAction.comment_delete,
            target_id=comment_id,
            details={
                "video_id": str(video_id),
                "author_id": str(comment.author_id),
                "body": comment.body,
            },
        )
    await db.commit()
