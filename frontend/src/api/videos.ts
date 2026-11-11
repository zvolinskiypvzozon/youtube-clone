import { request } from "./client";
import type { VideoPage } from "./types";

export const FEED_PAGE_SIZE = 24;
export const SEARCH_MAX = 100;

/** Лента главной; с q — поиск по названию. Пагинация — непрозрачный курсор. */
export function fetchFeed(q: string, cursor: string | null, signal?: AbortSignal): Promise<VideoPage> {
  return request<VideoPage>("/api/videos", {
    query: { q: q || null, cursor, limit: FEED_PAGE_SIZE },
    signal,
  });
}
