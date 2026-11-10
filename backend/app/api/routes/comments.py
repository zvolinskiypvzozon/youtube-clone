import uuid

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import err
from app.services import comments as comments_service

router = APIRouter(prefix="/comments", tags=["comments"])


@router.delete(
    "/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить комментарий (автор или владелец видео)",
    responses={
        401: err("Требуется вход"),
        403: err("Комментарий виден, но удалить его может только автор или владелец видео"),
        404: err("Комментарий не найден или нет доступа"),
        503: err("Видео сейчас изменяется — повторить (Retry-After)"),
    },
)
async def delete_comment(comment_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    await comments_service.delete_comment(db, user, comment_id)
