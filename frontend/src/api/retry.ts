// Политика повторов TanStack Query (решение архитектора 2026-11-11): 4xx не повторяем,
// кроме 429; 429 и 503 — с учётом Retry-After; сеть — один повтор.
import { ApiError, NetworkError } from "./client";

export const MAX_RETRIES = 3;
const MAX_DELAY_MS = 30_000;

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES) return false;
  if (error instanceof ApiError) return error.status === 429 || error.status === 503;
  if (error instanceof NetworkError) return failureCount < 1;
  return false;
}

/** Мутации: повтор только на 429 (nginx отклонил запрос до API). */
export function shouldRetryMutation(failureCount: number, error: unknown): boolean {
  return failureCount < MAX_RETRIES && error instanceof ApiError && error.status === 429;
}

export function retryDelay(attempt: number, error: unknown): number {
  if (error instanceof ApiError && error.retryAfter !== null) {
    // Не меньше 1 с: Retry-After: 0 не должен давать мгновенные повторы (аудит дня 7).
    return Math.min(Math.max(error.retryAfter * 1000, 1000), MAX_DELAY_MS);
  }
  return Math.min(1000 * 2 ** attempt, MAX_DELAY_MS);
}
