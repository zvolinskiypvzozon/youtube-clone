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
  if (wasLoggedIn) void resetSessionData(client);
}

/**
 * Открытая страница видео при смене сессии не сбрасывается, а перезапрашивается: иначе плеер
 * размонтировался бы — позиция потеряна, просмотр засчитан повторно (ревью дня 8). Ответ под
 * новой сессией заменит my_reaction и state; 404 — страница «Видео недоступно». Ссылки потока
 * не перезапрашиваются (смена src сбросила бы буфер): при истечении плеер запросит их сам.
 */
const KEEP_ACTIVE = new Set<unknown>(["video", "stream"]);

/** Сброс данных прежней сессии (вход, выход, 401). */
export async function resetSessionData(client: QueryClient): Promise<void> {
  const kept = (key: readonly unknown[]) => KEEP_ACTIVE.has(key[0]);
  // Неактивные карточки и ссылки прежней сессии — удалить: при следующем открытии они
  // показали бы чужие my_reaction/state до ответа сервера.
  client.removeQueries({ predicate: (q) => kept(q.queryKey) && !q.isActive() });
  await client.resetQueries({ predicate: (q) => q.queryKey[0] !== ME_KEY[0] && !kept(q.queryKey) });
  await client.invalidateQueries({ queryKey: ["video"] });
}
