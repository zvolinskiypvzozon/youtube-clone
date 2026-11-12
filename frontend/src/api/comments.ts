import { request } from "./client";
import type { BanOut, Comment, CommentPage } from "./types";

export const COMMENTS_PAGE_SIZE = 20;
/** Как COMMENT_MAX в API (CHECK body_length). */
export const COMMENT_MAX = 2000;
/** Как ModerationReason в API. */
export const BAN_REASON_MAX = 500;

export const commentsKey = (videoId: string) => ["comments", videoId] as const;

export const fetchComments = (videoId: string, cursor: string | null, signal?: AbortSignal) =>
  request<CommentPage>(`/api/videos/${encodeURIComponent(videoId)}/comments`, {
    query: { cursor, limit: COMMENTS_PAGE_SIZE },
    signal,
  });

export const addComment = (videoId: string, body: string) =>
  request<Comment>(`/api/videos/${encodeURIComponent(videoId)}/comments`, { method: "POST", body: { body } });

/** Автор, владелец видео или staff (решает сервер; can_delete — подсказка интерфейсу). */
export const deleteComment = (commentId: string) =>
  request<void>(`/api/comments/${encodeURIComponent(commentId)}`, { method: "DELETE" });

/** Бан автора из модерации (staff). Права по ролям проверяет сервер. */
export const banUser = (userId: string, reason: string) =>
  request<BanOut>(`/api/admin/users/${encodeURIComponent(userId)}/ban`, { method: "POST", body: { reason } });
