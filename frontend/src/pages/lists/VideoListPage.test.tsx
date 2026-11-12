import { fireEvent, screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireAuth } from "../../auth/RequireAuth";
import { json, ME, mockApi, renderApp, videoItem } from "../../test/utils";
import { LikedPage, SubscriptionsPage } from "./VideoListPage";

function render(route: string) {
  return renderApp(
    <Routes>
      <Route path="/feed/subscriptions" element={<RequireAuth><SubscriptionsPage /></RequireAuth>} />
      <Route path="/liked" element={<RequireAuth><LikedPage /></RequireAuth>} />
      <Route path="/login" element={<p>Страница входа</p>} />
    </Routes>,
    { route },
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Лента подписок и «Понравившиеся»", () => {
  it("лента подписок — из /api/me/feed, «Показать ещё» передаёт курсор", async () => {
    mockApi({
      "GET /api/auth/me": () => ME,
      "GET /api/me/feed": (url) =>
        url.searchParams.get("cursor") === "c2"
          ? { items: [videoItem(3)], next_cursor: null }
          : { items: [videoItem(1)], next_cursor: "c2" },
    });
    render("/feed/subscriptions");
    expect(await screen.findByRole("heading", { name: "Подписки" })).toBeInTheDocument();
    expect(await screen.findByText("Видео 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));
    expect(await screen.findByText("Видео 3")).toBeInTheDocument();
  });

  it("«Понравившиеся» — из /api/me/liked; пусто — подсказка", async () => {
    mockApi({ "GET /api/auth/me": () => ME, "GET /api/me/liked": () => ({ items: [], next_cursor: null }) });
    render("/liked");
    expect(await screen.findByRole("heading", { name: "Понравившиеся" })).toBeInTheDocument();
    expect(await screen.findByText("Понравившихся видео пока нет")).toBeInTheDocument();
  });

  it("ошибка — сообщение и «Повторить»", async () => {
    let fail = true;
    mockApi({
      "GET /api/auth/me": () => ME,
      "GET /api/me/liked": () => (fail ? json({ detail: "x" }, 500) : { items: [videoItem(5)], next_cursor: null }),
    });
    render("/liked");
    expect(await screen.findByText("Не удалось загрузить видео")).toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByText("Видео 5")).toBeInTheDocument();
  });

  it.each(["/feed/subscriptions", "/liked"])("%s — аноним уходит на вход, запросов к спискам нет", async (route) => {
    const list = vi.fn(() => ({ items: [], next_cursor: null }));
    mockApi({
      "GET /api/auth/me": () => json({ detail: "Требуется вход" }, 401),
      "GET /api/me/feed": list,
      "GET /api/me/liked": list,
    });
    render(route);
    expect(await screen.findByText("Страница входа")).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });
});
