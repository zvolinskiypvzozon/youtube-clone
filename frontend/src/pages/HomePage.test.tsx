import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { json, mockApi, renderApp, videoItem } from "../test/utils";
import { HomePage } from "./HomePage";

describe("HomePage", () => {
  // jsdom без IntersectionObserver: подгрузка — по кнопке «Показать ещё».
  it("карточки ленты и подгрузка следующей страницы по курсору", async () => {
    const fetchMock = mockApi({
      "GET /api/auth/me": () => new Response(null, { status: 401 }),
      "GET /api/videos": (url) =>
        url.searchParams.get("cursor") === "c2"
          ? { items: [videoItem(3)], next_cursor: null }
          : { items: [videoItem(1), videoItem(2)], next_cursor: "c2" },
    });
    renderApp(<HomePage />);
    expect(await screen.findByText("Видео 1")).toBeInTheDocument();
    expect(screen.getByText("Видео 2")).toBeInTheDocument();
    expect(screen.getAllByText("1:05")).toHaveLength(2);
    expect(screen.getByText(/2 просмотра/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));
    expect(await screen.findByText("Видео 3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать ещё" })).toBeNull();
    const feedCalls = fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.startsWith("/api/videos"));
    expect(feedCalls).toEqual(["/api/videos?limit=24", "/api/videos?cursor=c2&limit=24"]);
  });

  it("поиск: q уходит в API, заголовок с запросом, пустой результат", async () => {
    const fetchMock = mockApi({
      "GET /api/auth/me": () => new Response(null, { status: 401 }),
      "GET /api/videos": () => ({ items: [], next_cursor: null }),
    });
    renderApp(<HomePage />, { route: "/?q=<b>кот</b>" });
    expect(await screen.findByText("Ничего не найдено")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Результаты по запросу «<b>кот</b>»");
    const call = fetchMock.mock.calls.map(([u]) => String(u)).find((u) => u.startsWith("/api/videos"));
    expect(new URL(call!, "http://x").searchParams.get("q")).toBe("<b>кот</b>");
  });

  it("ошибка загрузки → сообщение и «Повторить»", async () => {
    let fail = true;
    mockApi({
      "GET /api/auth/me": () => new Response(null, { status: 401 }),
      "GET /api/videos": () => (fail ? json({ detail: "x" }, 500) : { items: [videoItem(1)], next_cursor: null }),
    });
    renderApp(<HomePage />);
    expect(await screen.findByText("Не удалось загрузить видео")).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(screen.getByText("Видео 1")).toBeInTheDocument());
  });

  it("ошибка подгрузки продолжения не зацикливает запросы (IntersectionObserver)", async () => {
    // Наблюдатель, который сразу сообщает «сторож виден» — как в браузере у конца ленты.
    class InstantObserver {
      constructor(private cb: IntersectionObserverCallback) {}
      observe() {
        this.cb([{ isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
      }
      disconnect() {}
      unobserve() {}
      takeRecords() {
        return [];
      }
    }
    vi.stubGlobal("IntersectionObserver", InstantObserver);
    const fetchMock = mockApi({
      "GET /api/auth/me": () => new Response(null, { status: 401 }),
      "GET /api/videos": (url) =>
        url.searchParams.get("cursor") ? json({ detail: "x" }, 500) : { items: [videoItem(1)], next_cursor: "c2" },
    });
    renderApp(<HomePage />);
    expect(await screen.findByText("Не удалось загрузить продолжение")).toBeInTheDocument();
    expect(screen.getByText("Видео 1")).toBeInTheDocument(); // загруженное остаётся
    await new Promise((r) => setTimeout(r, 200));
    const nextCalls = () => fetchMock.mock.calls.filter(([u]) => String(u).includes("cursor=")).length;
    expect(nextCalls()).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(nextCalls()).toBe(2));
    await new Promise((r) => setTimeout(r, 200));
    expect(nextCalls()).toBe(2);
  });

  it("запрос из адреса длиннее 100 символов обрезается (без 422)", async () => {
    const fetchMock = mockApi({
      "GET /api/auth/me": () => new Response(null, { status: 401 }),
      "GET /api/videos": () => ({ items: [], next_cursor: null }),
    });
    renderApp(<HomePage />, { route: `/?q=${"я".repeat(150)}` });
    expect(await screen.findByText("Ничего не найдено")).toBeInTheDocument();
    const call = fetchMock.mock.calls.map(([u]) => String(u)).find((u) => u.startsWith("/api/videos"));
    expect(new URL(call!, "http://x").searchParams.get("q")).toHaveLength(100);
  });
});

