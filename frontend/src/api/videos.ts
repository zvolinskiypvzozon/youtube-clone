import { api } from "./typed";
import type { ReactionKind, ReportIn } from "./types";

export const FEED_PAGE_SIZE = 24;
export const SEARCH_MAX = 100;

export const videoKey = (id: string) => ["video", id] as const;

const p = (video_id: string) => ({ params: { video_id } });

/** Лента главной; с q — поиск по названию. Пагинация — непрозрачный курсор. */
export const fetchFeed = (q: string, cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/videos", { query: { q: q || null, cursor, limit: FEED_PAGE_SIZE }, signal });

/** Карточка видео. 404 — нет, приватное без доступа, скрытое или автор забанен (не различаем). */
export const fetchVideo = (id: string, signal?: AbortSignal) => api("get", "/api/videos/{video_id}", { ...p(id), signal });

/** Presigned-ссылки на поток (короткий TTL): при ошибке источника плеер запрашивает заново. */
export const fetchStream = (id: string, signal?: AbortSignal) =>
  api("get", "/api/videos/{video_id}/stream", { ...p(id), signal });

/** Реакция: kind — поставить, null — снять. Счётчики — из ответа сервера. */
export const setReaction = (id: string, kind: ReactionKind | null) =>
  kind
    ? api("put", "/api/videos/{video_id}/reaction", { ...p(id), body: { kind } })
    : api("delete", "/api/videos/{video_id}/reaction", p(id));

/** Засчитать просмотр (плеер — после порога фактического воспроизведения). */
export const addView = (id: string) => api("post", "/api/videos/{video_id}/view", p(id));

export const createReport = (id: string, data: ReportIn) =>
  api("post", "/api/videos/{video_id}/reports", { ...p(id), body: data });
