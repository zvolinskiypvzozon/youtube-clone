import { request } from "./client";
import type {
  ReactionKind,
  ReactionOut,
  ReportCreatedOut,
  ReportIn,
  VideoCard,
  VideoPage,
  VideoStream,
} from "./types";

export const FEED_PAGE_SIZE = 24;
export const SEARCH_MAX = 100;

const videoPath = (id: string) => `/api/videos/${encodeURIComponent(id)}`;

/** Лента главной; с q — поиск по названию. Пагинация — непрозрачный курсор. */
export function fetchFeed(q: string, cursor: string | null, signal?: AbortSignal): Promise<VideoPage> {
  return request<VideoPage>("/api/videos", {
    query: { q: q || null, cursor, limit: FEED_PAGE_SIZE },
    signal,
  });
}

/** Карточка видео. 404 — нет, приватное без доступа, скрытое или автор забанен (не различаем). */
export const fetchVideo = (id: string, signal?: AbortSignal) => request<VideoCard>(videoPath(id), { signal });

/** Presigned-ссылки на поток (короткий TTL): при ошибке источника плеер запрашивает заново. */
export const fetchStream = (id: string, signal?: AbortSignal) =>
  request<VideoStream>(`${videoPath(id)}/stream`, { signal });

/** Реакция: kind — поставить, null — снять. Счётчики — из ответа сервера. */
export const setReaction = (id: string, kind: ReactionKind | null) =>
  request<ReactionOut>(`${videoPath(id)}/reaction`, kind ? { method: "PUT", body: { kind } } : { method: "DELETE" });

/** Засчитать просмотр (плеер — после порога фактического воспроизведения). */
export const addView = (id: string) => request<void>(`${videoPath(id)}/view`, { method: "POST" });

export const createReport = (id: string, data: ReportIn) =>
  request<ReportCreatedOut>(`${videoPath(id)}/reports`, { method: "POST", body: data });

export const videoKey = (id: string) => ["video", id] as const;
