import type { QueryClient } from "@tanstack/react-query";
import { PLAYLIST_LIST_KEYS } from "./playlists";
import { api } from "./typed";
import type { ModerationState, ReportStatus } from "./types";

export const REPORTS_PAGE_SIZE = 20;
/** Как ModerationReason в API: причина скрытия, отклонения и бана — 1–500 символов. */
export const MODERATION_REASON_MAX = 500;

export const reportsKey = (status: ReportStatus) => ["reports", status] as const;

/** Очередь жалоб (staff). Модератору жалобы на его видео сервер не показывает. */
export const fetchReports = (status: ReportStatus, cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/admin/reports", { query: { status, cursor, limit: REPORTS_PAGE_SIZE }, signal });

/** Отклонить жалобу; причина необязательна. */
export const dismissReport = (reportId: string, reason?: string) =>
  api("post", "/api/admin/reports/{report_id}/dismiss", {
    params: { report_id: reportId },
    body: reason ? { reason } : undefined,
  });

/** Скрыть (причина обязательна — её увидит владелец) или вернуть видео. */
export const setVideoModeration = (videoId: string, state: ModerationState, reason?: string) =>
  api("put", "/api/admin/videos/{video_id}/moderation", {
    params: { video_id: videoId },
    body: { state, reason: reason ?? null },
  });

/** Снять бан. Бан, наложенный админом, модератор снять не может (403). */
export const unbanUser = (userId: string) =>
  api("delete", "/api/admin/users/{user_id}/ban", { params: { user_id: userId } });

/**
 * Что устаревает после действия модерации (скрытие, возврат, отклонение, бан, разбан): очереди
 * жалоб (жалобы переходят между статусами; в снимке — owner_banned), карточки и списки видео,
 * комментарии всех видео (пометки и выдача забаненного), каналы, плейлисты, «Мои видео».
 * Одна функция для страницы модерации и для бана/разбана из комментариев (ревью дня 8).
 */
export function refreshAfterModeration(queryClient: QueryClient) {
  return Promise.all(
    [
      ["reports"],
      ["video"],
      ["videos"],
      ["comments"],
      ["channel"],
      ["channel-videos"],
      ["my-videos"],
      ...PLAYLIST_LIST_KEYS,
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}
