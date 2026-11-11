import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";
import { retryDelay, shouldRetry, shouldRetryMutation } from "./retry";

export const ME_KEY = ["me"] as const;

export function createQueryClient(): QueryClient {
  const client: QueryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry, retryDelay, staleTime: 30_000, refetchOnWindowFocus: false },
      // Мутации — только 429: запрос точно не обработан. 503 мог прийти уже после записи
      // (прокси) — повтор неидемпотентного POST дал бы дубль (ревью дня 7).
      mutations: { retry: shouldRetryMutation, retryDelay },
    },
    // 401 на любом запросе — сессия истекла или отозвана: показываем «аноним».
    queryCache: new QueryCache({ onError: (error) => onUnauthorized(client, error) }),
    mutationCache: new MutationCache({ onError: (error) => onUnauthorized(client, error) }),
  });
  return client;
}

/**
 * 401 — сессия истекла или отозвана: интерфейс анонима и сброс данных прежней сессии
 * (аудит дня 7). Сброс — только при переходе «вошёл → аноним»: иначе запрос, которому нужен
 * вход, после сброса снова получил бы 401 — бесконечный цикл.
 */
function onUnauthorized(client: QueryClient, error: unknown): void {
  if (!(error instanceof ApiError && error.status === 401)) return;
  const wasLoggedIn = Boolean(client.getQueryData(ME_KEY));
  client.setQueryData(ME_KEY, null);
  if (wasLoggedIn) void client.resetQueries({ predicate: (q) => q.queryKey[0] !== ME_KEY[0] });
}
