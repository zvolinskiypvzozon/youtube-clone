import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, type Handler, type MockRoutes } from "../../test/utils";
import { changedFields, MY_VIDEOS_POLL_MS, MyVideosPage } from "./MyVideosPage";

function item(n: number, state: Record<string, unknown> = {}) {
  return {
    id: `v${n}`,
    title: `Видео ${n}`,
    duration_seconds: 65,
    views_count: n,
    created_at: "2026-11-11T12:00:00Z",
    published_at: null,
    thumbnail_url: null,
    state: { status: "ready", visibility: "public", moderation: "ok", moderation_reason: null, failure_reason: null, ...state },
  };
}

function card(n: number, extra: Record<string, unknown> = {}) {
  return {
    id: `v${n}`,
    title: `Видео ${n}`,
    description: `Описание ${n}`,
    channel: ME.channel,
    duration_seconds: 65,
    views_count: 0,
    likes_count: 0,
    dislikes_count: 0,
    comments_count: 0,
    published_at: null,
    thumbnail_url: null,
    state: null,
    ...extra,
  };
}

function api(routes: MockRoutes = {}) {
  return mockApi({
    "GET /api/auth/me": () => ME,
    "GET /api/me/videos": () => ({ items: [item(1)], next_cursor: null }),
    "GET /api/videos/v1": () => card(1),
    ...routes,
  });
}

const render = () => renderApp(<MyVideosPage />, { route: "/my/videos" });
const row = async (title: string) => (await screen.findByText(title)).closest("li") as HTMLElement;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("changedFields", () => {
  it("только изменённые поля", () => {
    const base = { title: "a", description: "b", visibility: "public" as const };
    expect(changedFields(base, base)).toEqual({});
    expect(changedFields(base, { ...base, title: "x", visibility: "private" })).toEqual({ title: "x", visibility: "private" });
  });
});

describe("MyVideosPage: список", () => {
  it("статусы, доступ и причины — текстом", async () => {
    api({
      "GET /api/me/videos": () => ({
        items: [
          item(1, { status: "processing" }),
          item(2, { status: "failed", failure_reason: "Файл не является видео" }),
          item(3, { moderation: "hidden", moderation_reason: "<b>спам</b>" }),
          item(4, { visibility: "private" }),
        ],
        next_cursor: null,
      }),
    });
    const { container } = render();
    expect(within(await row("Видео 1")).getByText("Обрабатывается")).toBeInTheDocument();
    expect(within(await row("Видео 2")).getByText("Файл не является видео")).toBeInTheDocument();
    const hidden = await row("Видео 3");
    expect(within(hidden).getByText("Скрыто модерацией")).toBeInTheDocument();
    expect(within(hidden).getByText("<b>спам</b>")).toHaveAttribute("dir", "auto");
    expect(container.querySelector("b")).toBeNull();
    expect(within(await row("Видео 4")).getByText("Ограниченный доступ")).toBeInTheDocument();
  });

  it("пусто — ссылка на загрузку", async () => {
    api({ "GET /api/me/videos": () => ({ items: [], next_cursor: null }) });
    render();
    expect(await screen.findByText("У вас пока нет видео")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Загрузите первое видео" })).toHaveAttribute("href", "/upload");
  });

  it("пока есть processing — опрос; готово — опрос прекращается; uploading — не опрашивается", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    api({
      "GET /api/me/videos": () => {
        calls++;
        return { items: [item(1, { status: calls < 2 ? "processing" : "ready" }), item(2, { status: "uploading" })], next_cursor: null };
      },
    });
    render();
    expect(await screen.findByText("Обрабатывается")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(MY_VIDEOS_POLL_MS);
    expect(await screen.findByText("Готово")).toBeInTheDocument();
    const after = calls;
    await vi.advanceTimersByTimeAsync(MY_VIDEOS_POLL_MS * 3);
    expect(calls).toBe(after);
  });
});

describe("MyVideosPage: правка", () => {
  it("уходят только изменённые поля; после сохранения список перезапрошен", async () => {
    let listCalls = 0;
    const patch = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ title: "Новое", visibility: "private" });
      return card(1, { title: "Новое" });
    });
    api({
      "GET /api/me/videos": () => (listCalls++, { items: [item(1)], next_cursor: null }),
      "PATCH /api/videos/v1": patch,
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Изменить" }));
    const title = await screen.findByLabelText("Название");
    expect(title).toHaveValue("Видео 1");
    expect(title).toHaveFocus();
    expect(screen.getByLabelText("Описание")).toHaveValue("Описание 1");
    fireEvent.change(title, { target: { value: "Новое" } });
    fireEvent.click(screen.getByLabelText("Ограниченный доступ"));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByLabelText("Название")).not.toBeInTheDocument());
    expect(listCalls).toBe(2);
  });

  it("без изменений — запроса нет, форма закрывается", async () => {
    const patch = vi.fn();
    api({ "PATCH /api/videos/v1": patch });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Изменить" }));
    fireEvent.click(await screen.findByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(screen.queryByLabelText("Название")).not.toBeInTheDocument());
    expect(patch).not.toHaveBeenCalled();
  });

  it("422 — ошибка у поля, форма остаётся", async () => {
    api({
      "PATCH /api/videos/v1": () =>
        json({ detail: "Ошибка проверки", errors: [{ loc: ["body", "title"], msg: "Название без видимых символов" }] }, 422),
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Изменить" }));
    fireEvent.change(await screen.findByLabelText("Название"), { target: { value: "​" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("Название без видимых символов")).toBeInTheDocument();
    expect(screen.getByLabelText("Название")).toBeInTheDocument();
  });
});

describe("MyVideosPage: правка — прочие ошибки", () => {
  it("403/409 — общее сообщение, форма остаётся", async () => {
    api({ "PATCH /api/videos/v1": () => json({ detail: "Управлять может только владелец" }, 403) });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Изменить" }));
    fireEvent.change(await screen.findByLabelText("Название"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await screen.findByText("Управлять может только владелец")).toBeInTheDocument();
    expect(screen.getByLabelText("Название")).toBeInTheDocument();
  });
});

describe("MyVideosPage: опрос при ошибке", () => {
  it("ошибка перезапроса останавливает опрос", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    api({
      "GET /api/me/videos": () =>
        ++calls === 1 ? { items: [item(1, { status: "processing" })], next_cursor: null } : json({ detail: "x" }, 500),
    });
    render();
    await screen.findByText("Обрабатывается");
    await vi.advanceTimersByTimeAsync(MY_VIDEOS_POLL_MS);
    await waitFor(() => expect(calls).toBe(2));
    await vi.advanceTimersByTimeAsync(MY_VIDEOS_POLL_MS * 3);
    expect(calls).toBe(2);
  });
});

describe("MyVideosPage: удаление", () => {
  it("подтверждение (фокус на нём) → DELETE → список перезапрошен", async () => {
    let deleted = false;
    const del = vi.fn(() => ((deleted = true), new Response(null, { status: 204 })));
    api({
      "GET /api/me/videos": () => ({ items: deleted ? [] : [item(1)], next_cursor: null }),
      "DELETE /api/videos/v1": del,
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Удалить" }));
    const confirm = screen.getByRole("button", { name: "Удалить навсегда" });
    expect(confirm).toHaveFocus();
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    expect(await screen.findByText("У вас пока нет видео")).toBeInTheDocument();
    expect(del).toHaveBeenCalledTimes(1);
  });

  it("503 (обрабатывается) — сообщение, видео остаётся; «Отмена» убирает ошибку", async () => {
    api({ "DELETE /api/videos/v1": () => json({ detail: "x" }, 503, { "Retry-After": "2" }) });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Удалить" }));
    fireEvent.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    expect(await screen.findByText(/Сервер занят/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(screen.queryByText(/Сервер занят/)).not.toBeInTheDocument();
    expect(screen.getByText("Видео 1")).toBeInTheDocument();
  });
});

describe("MyVideosPage: доступ к приватному", () => {
  const privateList: Handler = () => ({ items: [item(1, { visibility: "private" })], next_cursor: null });
  const entry = { user_id: "u2", username: "bob", granted_at: "2026-11-11T12:00:00Z" };

  it("список, открыть по нику, закрыть доступ", async () => {
    let entries = [entry];
    const post = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ username: "carol_1" });
      entries = [{ user_id: "u3", username: "carol_1", granted_at: "2026-11-12T12:00:00Z" }, ...entries];
      return json(entries[0], 201);
    });
    const del = vi.fn(() => ((entries = entries.filter((e) => e.user_id !== "u2")), new Response(null, { status: 204 })));
    api({
      "GET /api/me/videos": privateList,
      "GET /api/videos/v1/access": () => ({ items: entries, max_entries: 100 }),
      "POST /api/videos/v1/access": post,
      "DELETE /api/videos/v1/access/u2": del,
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Доступ" }));
    expect(await screen.findByText("bob")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Ник пользователя"), { target: { value: " carol_1 " } });
    fireEvent.click(screen.getByRole("button", { name: "Открыть доступ" }));
    expect(await screen.findByText("carol_1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Закрыть доступ: bob" }));
    await waitFor(() => expect(screen.queryByText("bob")).not.toBeInTheDocument());
    expect(del).toHaveBeenCalledTimes(1);
  });

  it("неверный ник — ошибка без запроса; 404 сервера — текстом", async () => {
    const post = vi.fn(() => json({ detail: "Пользователь не найден" }, 404));
    api({
      "GET /api/me/videos": privateList,
      "GET /api/videos/v1/access": () => ({ items: [], max_entries: 100 }),
      "POST /api/videos/v1/access": post,
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Доступ" }));
    await screen.findByText("Доступ пока никому не открыт.");
    fireEvent.change(screen.getByLabelText("Ник пользователя"), { target: { value: "ab" } });
    fireEvent.click(screen.getByRole("button", { name: "Открыть доступ" }));
    expect(screen.getByText(/3–30 символов/)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Ник пользователя"), { target: { value: "nobody" } });
    fireEvent.click(screen.getByRole("button", { name: "Открыть доступ" }));
    expect(await screen.findByText("Пользователь не найден")).toBeInTheDocument();
  });

  it("«Доступ» у неприватного видео — с пояснением, что список действует только для ограниченного", async () => {
    api({ "GET /api/videos/v1/access": () => ({ items: [entry], max_entries: 100 }) });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Доступ" }));
    expect(await screen.findByText(/Список действует, только когда у видео ограниченный доступ/)).toBeInTheDocument();
    expect(await screen.findByText("bob")).toBeInTheDocument();
  });

  it("ошибка закрытия доступа — текстом, запись остаётся", async () => {
    api({
      "GET /api/me/videos": privateList,
      "GET /api/videos/v1/access": () => ({ items: [entry], max_entries: 100 }),
      "DELETE /api/videos/v1/access/u2": () => json({ detail: "Видео сейчас изменяется" }, 409),
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Доступ" }));
    fireEvent.click(await screen.findByRole("button", { name: "Закрыть доступ: bob" }));
    expect(await screen.findByText("Видео сейчас изменяется")).toBeInTheDocument();
    expect(screen.getByText("bob")).toBeInTheDocument();
  });

  it("лимит списка — кнопка неактивна", async () => {
    api({
      "GET /api/me/videos": privateList,
      "GET /api/videos/v1/access": () => ({ items: [entry], max_entries: 1 }),
    });
    render();
    fireEvent.click(within(await row("Видео 1")).getByRole("button", { name: "Доступ" }));
    expect(await screen.findByText(/Достигнут лимит списка/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть доступ" })).toBeDisabled();
  });
});
