import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { vi } from "vitest";
import { createQueryClient } from "../api/queryClient";
import { AuthProvider } from "../auth/AuthContext";

export type Handler = (url: URL, init: RequestInit) => Response | object | Promise<Response | object>;

/** Подменяет fetch: ключ "METHOD /path" → ответ (объект — JSON 200). Нет ключа — 404. */
export function mockApi(routes: Record<string, Handler>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const key = `${(init.method ?? "GET").toUpperCase()} ${url.pathname}`;
    const handler = routes[key];
    if (!handler) return json({ detail: "Не найдено" }, 404);
    const result = await handler(url, init);
    return result instanceof Response ? result : json(result);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

export function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

export function renderApp(ui: ReactElement, { route = "/", state }: { route?: string; state?: unknown } = {}) {
  const client = createQueryClient();
  // В тестах — без повторов и задержек (политику повторов проверяет retry.test.ts).
  // Остальные настройки — как в приложении (refetchOnWindowFocus, staleTime): иначе тесты
  // проверяли бы поведение, которого в приложении нет.
  const defaults = client.getDefaultOptions();
  client.setDefaultOptions({
    queries: { ...defaults.queries, retry: false },
    mutations: { ...defaults.mutations, retry: false },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: route.split("?")[0], search: route.includes("?") ? `?${route.split("?")[1]}` : "", state }]}>
        <AuthProvider>
          {ui}
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export const ME = {
  id: "u1",
  email: "alice@example.com",
  username: "alice",
  role: "user",
  created_at: "2026-11-01T00:00:00Z",
  channel: { id: "c1", handle: "alice", title: "alice" },
} as const;

export function videoItem(n: number) {
  return {
    id: `v${n}`,
    title: `Видео ${n}`,
    channel: { id: "c1", handle: "alice", title: "Канал Алисы" },
    duration_seconds: 65,
    views_count: n,
    published_at: "2026-11-10T12:00:00Z",
    thumbnail_url: `/yt-media/videos/v${n}/thumb.jpg`,
  };
}
