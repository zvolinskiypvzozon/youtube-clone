// Завершение загрузки (complete) с повтором. API сделан повторяемым: части склеены, а commit
// не прошёл — повтор продолжит; второй complete после успешного получит 409 (ревью дня 8).
// Поэтому временная ошибка (сеть, 5xx, 429) не повод сразу удалять загрузку: повторяем, а на
// 409 и после последней неудачи сверяем состояние видео с сервером.
import { ApiError, NetworkError } from "../api/client";
import type { CompletedPart } from "../api/types";
import { completeUpload } from "../api/uploads";
import { fetchVideo } from "../api/videos";

export const COMPLETE_ATTEMPTS = 3;
export const COMPLETE_RETRY_DELAY_MS = 1000;

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function transient(error: unknown): boolean {
  return error instanceof NetworkError || (error instanceof ApiError && (error.status >= 500 || error.status === 429));
}

/** Видео уже в обработке (прошлый complete успел)? Ошибка проверки — «не знаем» (false). */
async function alreadyCompleted(videoId: string): Promise<boolean> {
  try {
    const status = (await fetchVideo(videoId)).state?.status;
    return status === "processing" || status === "ready";
  } catch {
    return false;
  }
}

/** Успех — видео ушло в обработку. Исключение — окончательная ошибка (загрузку можно удалять). */
export async function finishUpload(
  videoId: string,
  parts: CompletedPart[],
  sleep: (ms: number) => Promise<void> = defaultSleep,
): Promise<void> {
  for (let attempt = 1; ; attempt++) {
    try {
      await completeUpload(videoId, parts);
      return;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      if (conflict || (transient(error) && attempt >= COMPLETE_ATTEMPTS)) {
        if (await alreadyCompleted(videoId)) return;
        throw error;
      }
      if (!transient(error)) throw error;
      await sleep(COMPLETE_RETRY_DELAY_MS * attempt);
    }
  }
}
