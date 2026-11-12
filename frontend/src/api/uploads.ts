import { api } from "./typed";
import type { CompletedPart, UploadCreateIn } from "./types";

/** Как MAX_UPLOAD_BYTES в API (ADR 0004): 2 ГБ. */
export const MAX_UPLOAD_BYTES = 2 * 1024 ** 3;
export const TITLE_MAX = 100;
export const DESCRIPTION_MAX = 5000;

/** Видео в статусе uploading и presigned-ссылки на части (длина каждой — в подписи). */
export const startUpload = (data: UploadCreateIn) => api("post", "/api/uploads", { body: data });

export const completeUpload = (videoId: string, parts: CompletedPart[]) =>
  api("post", "/api/uploads/{video_id}/complete", { params: { video_id: videoId }, body: { parts } });

export const cancelUpload = (videoId: string) =>
  api("delete", "/api/uploads/{video_id}", { params: { video_id: videoId } });
