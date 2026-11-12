import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, videoItem, type Handler, type MockRoutes } from "../../test/utils";
import { ChannelPage } from "./ChannelPage";

const BOB = {
  id: "c2",
  handle: "bob",
  title: "Канал שלום",
  description: "<b>описание</b>",
  subscribers_count: 3,
  created_at: "2026-11-01T00:00:00Z",
  subscribed: false,
};
const OWN = { ...BOB, id: ME.channel.id, handle: ME.channel.handle, title: ME.channel.title };

function playlist(n: number, extra: Record<string, unknown> = {}) {
  return { id: `p${n}`, title: `Плейлист ${n}`, video_count: n, thumbnail_url: null, visibility: null, created_at: "2026-11-10T00:00:00Z", ...extra };
}

const anon: Handler = () => json({ detail: "Требуется вход" }, 401);

function api(routes: MockRoutes = {}, { user = ME as object | null } = {}) {
  return mockApi({
    "GET /api/auth/me": user ? () => user : anon,
    "GET /api/channels/bob": () => BOB,
    "GET /api/channels/bob/videos": () => ({ items: [videoItem(1), videoItem(2)], next_cursor: null }),
    "GET /api/channels/bob/playlists": () => ({ items: [playlist(1)], next_cursor: null }),
    "GET /api/channels/alice": () => OWN,
    "GET /api/channels/alice/videos": () => ({ items: [], next_cursor: null }),
    "GET /api/me/playlists": () => ({ items: [playlist(7, { visibility: "private" })], next_cursor: null }),
    ...routes,
  });
}

function render(route: string) {
  return renderApp(
    <Routes>
      <Route path="/channel/:handle" element={<ChannelPage />} />
      <Route path="/playlist/:playlistId" element={<p>страница плейлиста</p>} />
    </Routes>,
    { route },
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ChannelPage", () => {
  it("шапка — текстом с dir=auto; чужой канал — подписка; видео канала", async () => {
    api();
    const { container } = render("/channel/bob");
    const title = await screen.findByRole("heading", { level: 1 });
    expect(title).toHaveTextContent(BOB.title);
    expect(title).toHaveAttribute("dir", "auto");
    expect(screen.getByText("<b>описание</b>")).toHaveAttribute("dir", "auto");
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText(/3 подписчика/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Подписаться" })).toBeInTheDocument();
    expect(await screen.findByText("Видео 1")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Видео" })).toHaveAttribute("aria-selected", "true");
  });

  it.each([
    [404, "Not found"],
    [422, "Проверьте правильность полей: handle"],
  ])("%i — «Канал не найден» без подробностей и «Повторить»", async (status, detail) => {
    api({ "GET /api/channels/bob": () => json({ detail }, status) });
    render("/channel/bob");
    expect(await screen.findByText("Канал не найден")).toBeInTheDocument();
    expect(screen.queryByText(detail)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
  });

  it("вкладки: в порядке Tab только активная, стрелки и Home/End переключают с фокусом", async () => {
    api();
    render("/channel/bob");
    const videos = await screen.findByRole("tab", { name: "Видео" });
    const playlists = screen.getByRole("tab", { name: "Плейлисты" });
    expect(videos).toHaveAttribute("tabindex", "0");
    expect(playlists).toHaveAttribute("tabindex", "-1");
    expect(playlists).not.toHaveAttribute("aria-controls");
    expect(document.getElementById(videos.getAttribute("aria-controls") ?? "")).toHaveAttribute("role", "tabpanel");
    fireEvent.keyDown(videos, { key: "ArrowRight" });
    expect(playlists).toHaveFocus();
    expect(playlists).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("location")).toHaveTextContent("/channel/bob?tab=playlists");
    fireEvent.keyDown(playlists, { key: "ArrowRight" });
    expect(videos).toHaveFocus();
    fireEvent.keyDown(videos, { key: "End" });
    expect(playlists).toHaveFocus();
    fireEvent.keyDown(playlists, { key: "Home" });
    expect(videos).toHaveAttribute("aria-selected", "true");
  });

  it("«Показать ещё» передаёт курсор", async () => {
    api({
      "GET /api/channels/bob/videos": (url) =>
        url.searchParams.get("cursor") === "c2"
          ? { items: [videoItem(3)], next_cursor: null }
          : { items: [videoItem(1)], next_cursor: "c2" },
    });
    render("/channel/bob");
    fireEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));
    expect(await screen.findByText("Видео 3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
  });

  it("свой канал, пока /me не ответил: открытые плейлисты канала не запрашиваются", async () => {
    const open = vi.fn(() => ({ items: [], next_cursor: null }));
    api({ "GET /api/auth/me": () => new Promise<object>(() => undefined), "GET /api/channels/alice/playlists": open });
    render("/channel/alice?tab=playlists");
    await screen.findByRole("heading", { level: 1 });
    await new Promise((r) => setTimeout(r, 50));
    expect(open).not.toHaveBeenCalled();
  });

  it("вкладка «Плейлисты» чужого канала — открытые плейлисты канала, без создания", async () => {
    const mine = vi.fn(() => ({ items: [], next_cursor: null }));
    api({ "GET /api/me/playlists": mine });
    render("/channel/bob?tab=playlists");
    expect(await screen.findByText("Плейлист 1")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Плейлисты" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: "Создать плейлист" })).not.toBeInTheDocument();
    expect(mine).not.toHaveBeenCalled();
  });

  it("переключение вкладок — в адресе", async () => {
    api();
    render("/channel/bob");
    fireEvent.click(await screen.findByRole("tab", { name: "Плейлисты" }));
    expect(await screen.findByText("Плейлист 1")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/channel/bob?tab=playlists");
  });

  it("свой канал: без подписки, ссылки на свои видео; плейлисты — все свои с видимостью", async () => {
    api();
    render("/channel/alice?tab=playlists");
    expect(await screen.findByText("Плейлист 7")).toBeInTheDocument();
    expect(screen.getByText("Только я")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подписаться" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Мои видео" })).toHaveAttribute("href", "/my/videos");
  });

  it("свой канал: создание плейлиста — POST и переход на него", async () => {
    const post = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ title: "Новый", description: "", visibility: "unlisted" });
      return json({ ...playlist(9), description: "", channel: ME.channel, visibility: "unlisted", can_manage: true, updated_at: "2026-11-12T00:00:00Z" }, 201);
    });
    api({ "POST /api/playlists": post });
    render("/channel/alice?tab=playlists");
    fireEvent.click(await screen.findByRole("button", { name: "Создать плейлист" }));
    fireEvent.change(screen.getByLabelText("Название плейлиста"), { target: { value: "Новый" } });
    fireEvent.click(screen.getByLabelText(/По ссылке/));
    fireEvent.click(within(screen.getByRole("form", { name: "Создать плейлист" })).getByRole("button", { name: "Создать плейлист" }));
    expect(await screen.findByText("страница плейлиста")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/playlist/p9");
  });

  it("создание: пустое название — ошибка у поля, без запроса; 422 — у поля", async () => {
    const post = vi.fn(() =>
      json({ detail: "x", errors: [{ loc: ["body", "title"], msg: "Название без видимых символов" }] }, 422),
    );
    api({ "POST /api/playlists": post });
    render("/channel/alice?tab=playlists");
    fireEvent.click(await screen.findByRole("button", { name: "Создать плейлист" }));
    const form = screen.getByRole("form", { name: "Создать плейлист" });
    fireEvent.click(within(form).getByRole("button", { name: "Создать плейлист" }));
    expect(screen.getByText("Введите название")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Название плейлиста"), { target: { value: "​" } });
    fireEvent.click(within(form).getByRole("button", { name: "Создать плейлист" }));
    expect(await screen.findByText("Название без видимых символов")).toBeInTheDocument();
  });

  it("пустой канал — понятное состояние", async () => {
    api();
    render("/channel/alice");
    expect(await screen.findByText("На канале пока нет видео")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("link", { name: "Все ваши видео" })).toBeInTheDocument());
  });
});
