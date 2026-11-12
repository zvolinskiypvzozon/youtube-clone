// Загрузка файла частями по presigned-ссылкам (ADR 0004): части режутся Blob.slice — файл
// целиком в память не читается (решение архитектора, день 8); 3 части параллельно; ETag
// каждой части — из заголовка ответа (тот же origin, CORS не нужен).
import type { CompletedPart, UploadPartUrl } from "../api/types";
import { safeUrl } from "./safeUrl";

export const UPLOAD_CONCURRENCY = 3;
/** Попыток на часть: сеть и 5xx повторяются, 4xx — нет. */
export const PART_ATTEMPTS = 3;
/** Пауза перед повтором части (умножается на номер попытки). */
export const PART_RETRY_DELAY_MS = 1000;
/** Части отправляются только в бакет загрузки — не на произвольный путь из ответа. */
const RAW_PREFIX = "/yt-raw/";
/** Таймаут одной части (16 МиБ): зависшее соединение — сетевой сбой, он повторяется. */
export const PART_TIMEOUT_MS = 5 * 60_000;

/**
 * Ссылка части — путь этого сайта внутри /yt-raw/ после того, как браузер разберёт её сам:
 * `..`, `%2e%2e` и `.%2E` схлопываются URL-парсером и вывели бы PUT на другой путь
 * (аудит дня 8). Сверяется разобранный путь, а не строка.
 */
export function isRawPartUrl(url: string): boolean {
  const safe = safeUrl(url);
  if (!safe) return false;
  const parsed = new URL(safe, window.location.origin);
  return parsed.origin === window.location.origin && parsed.pathname.startsWith(RAW_PREFIX) && safe.startsWith(RAW_PREFIX);
}

export class UploadPartError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "UploadPartError";
    this.status = status;
  }
}

export type PutPart = (
  url: string,
  body: Blob,
  onProgress: (loaded: number) => void,
  signal: AbortSignal,
) => Promise<string>;

function abortError(): DOMException {
  return new DOMException("Загрузка отменена", "AbortError");
}

/** PUT части через XHR (прогресс отправки есть только у XHR). Возвращает ETag. */
export const xhrPutPart: PutPart = (url, body, onProgress, signal) =>
  new Promise((resolve, reject) => {
    if (signal.aborted) return reject(abortError());
    const xhr = new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    signal.addEventListener("abort", onAbort, { once: true });
    const done = () => signal.removeEventListener("abort", onAbort);
    xhr.open("PUT", url);
    xhr.timeout = PART_TIMEOUT_MS;
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      done();
      const etag = xhr.getResponseHeader("ETag");
      if (xhr.status >= 200 && xhr.status < 300 && etag) resolve(etag);
      else reject(new UploadPartError(xhr.status, `Хранилище ответило ${xhr.status}`));
    };
    xhr.onerror = () => {
      done();
      reject(new UploadPartError(0, "Нет связи с сервером"));
    };
    xhr.ontimeout = () => {
      done();
      reject(new UploadPartError(0, "Сервер не отвечает"));
    };
    xhr.onabort = () => {
      done();
      reject(abortError());
    };
    // Тело — срез файла: Content-Type не ставится (подписана только длина).
    xhr.send(body);
  });

export interface UploadOptions {
  signal: AbortSignal;
  onProgress?: (loaded: number, total: number) => void;
  putPart?: PutPart;
  concurrency?: number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

/** Пауза, которую прерывает отмена (иначе «Отменить» ждало бы конца паузы перед повтором). */
const defaultSleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    if (signal.aborted) return reject(abortError());
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });

function retryable(error: unknown): boolean {
  // 0 — сеть; 5xx — сбой хранилища или прокси. 403 (истёкшая подпись, неверная длина) и
  // прочие 4xx повтор не исправит.
  return error instanceof UploadPartError && (error.status === 0 || error.status >= 500);
}

/** Загружает все части и возвращает [{number, etag}] по возрастанию номера. */
export async function uploadParts(file: Blob, parts: UploadPartUrl[], options: UploadOptions): Promise<CompletedPart[]> {
  const { onProgress, putPart = xhrPutPart, concurrency = UPLOAD_CONCURRENCY, sleep = defaultSleep } = options;
  const total = parts.reduce((sum, p) => sum + p.size, 0);
  if (total !== file.size) throw new Error("Размер частей не совпадает с размером файла");
  for (const part of parts) {
    if (!isRawPartUrl(part.url)) throw new Error("Недопустимая ссылка на загрузку части");
  }
  // Внутренняя отмена: первая окончательная ошибка части прерывает остальные (не грузить
  // впустую сотни мегабайт), внешняя — отмена пользователем.
  const inner = new AbortController();
  const signal = inner.signal;
  const onOuterAbort = () => inner.abort();
  if (options.signal.aborted) inner.abort();
  else options.signal.addEventListener("abort", onOuterAbort, { once: true });

  const offsets: number[] = [];
  parts.reduce((offset, p) => (offsets.push(offset), offset + p.size), 0);
  // Отправлено по каждой части (Map: присваивание по вычисляемому индексу запрещено ESLint).
  const loaded = new Map<number, number>();
  // После отмены или ошибки прогресс не сообщается: запоздалое событие части не должно
  // вернуть интерфейс в «загружается» (ревью дня 8).
  const report = () => {
    if (!signal.aborted) onProgress?.([...loaded.values()].reduce((a, b) => a + b, 0), total);
  };
  const results: CompletedPart[] = [];

  async function uploadOne(index: number): Promise<void> {
    const part = parts[index];
    const body = file.slice(offsets[index], offsets[index] + part.size);
    for (let attempt = 1; ; attempt++) {
      if (signal.aborted) throw abortError();
      try {
        const etag = await putPart(
          part.url,
          body,
          (n) => {
            loaded.set(index, Math.min(n, part.size));
            report();
          },
          signal,
        );
        loaded.set(index, part.size);
        report();
        results.push({ number: part.number, etag });
        return;
      } catch (error) {
        loaded.set(index, 0);
        report();
        if (signal.aborted || attempt >= PART_ATTEMPTS || !retryable(error)) throw error;
        await sleep(PART_RETRY_DELAY_MS * attempt, signal);
      }
    }
  }

  // Пул: не больше concurrency частей одновременно; первая ошибка останавливает выдачу новых.
  let next = 0;
  let failed: unknown = null;
  async function worker(): Promise<void> {
    while (failed === null && next < parts.length) {
      const index = next++;
      try {
        await uploadOne(index);
      } catch (error) {
        failed ??= error;
        inner.abort();
      }
    }
  }
  try {
    await Promise.all(Array.from({ length: Math.min(concurrency, parts.length) }, worker));
  } finally {
    options.signal.removeEventListener("abort", onOuterAbort);
  }
  if (failed !== null) throw failed;
  return results.sort((a, b) => a.number - b.number);
}

/** Сообщение об ошибке загрузки части для пользователя. */
export function uploadErrorMessage(error: unknown): string {
  if (error instanceof UploadPartError) {
    if (error.status === 0) return "Нет связи с сервером. Проверьте подключение и начните загрузку заново.";
    if (error.status === 403) return "Ссылки на загрузку истекли или файл изменился. Начните загрузку заново.";
    return `Не удалось загрузить файл (${error.status}). Начните загрузку заново.`;
  }
  return error instanceof Error ? error.message : "Не удалось загрузить файл.";
}
