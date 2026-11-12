import { request } from "./client";
import type { CompletedPart, UploadCreateIn, UploadCreateOut, UploadStatusOut } from "./types";

/** Как MAX_UPLOAD_BYTES в API (ADR 0004): 2 ГБ. */
export const MAX_UPLOAD_BYTES = 2 * 1024 ** 3;
export const TITLE_MAX = 100;
export const DESCRIPTION_MAX = 5000;

/** Видео в статусе uploading и presigned-ссылки на части (длина каждой — в подписи). */
export const startUpload = (data: UploadCreateIn) =>
  request<UploadCreateOut>("/api/uploads", { method: "POST", body: data });

export const completeUpload = (videoId: string, parts: CompletedPart[]) =>
  request<UploadStatusOut>(`/api/uploads/${encodeURIComponent(videoId)}/complete`, {
    method: "POST",
    body: { parts },
  });

export const cancelUpload = (videoId: string) =>
  request<void>(`/api/uploads/${encodeURIComponent(videoId)}`, { method: "DELETE" });
