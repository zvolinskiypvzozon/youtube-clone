import { api } from "./typed";

export const COMMENTS_PAGE_SIZE = 20;
/** Как COMMENT_MAX в API (CHECK body_length). */
export const COMMENT_MAX = 2000;
/** Как ModerationReason в API. */
export const BAN_REASON_MAX = 500;

export const commentsKey = (videoId: string) => ["comments", videoId] as const;

export const fetchComments = (videoId: string, cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/videos/{video_id}/comments", {
    params: { video_id: videoId },
    query: { cursor, limit: COMMENTS_PAGE_SIZE },
    signal,
  });

export const addComment = (videoId: string, body: string) =>
  api("post", "/api/videos/{video_id}/comments", { params: { video_id: videoId }, body: { body } });

/** Автор, владелец видео или staff (решает сервер; can_delete — подсказка интерфейсу). */
export const deleteComment = (commentId: string) =>
  api("delete", "/api/comments/{comment_id}", { params: { comment_id: commentId } });

/** Бан автора из модерации (staff): PUT по OpenAPI. Права по ролям проверяет сервер. */
export const banUser = (userId: string, reason: string) =>
  api("put", "/api/admin/users/{user_id}/ban", { params: { user_id: userId }, body: { reason } });
