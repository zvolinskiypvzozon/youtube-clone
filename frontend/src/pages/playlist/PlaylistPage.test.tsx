import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, type MockRoutes } from "../../test/utils";
import { changedPlaylistFields, PlaylistPage } from "./PlaylistPage";

const BOB = { id: "c2", handle: "bob", title: "Канал Боба" };

function playlist(extra: Record<string, unknown> = {}) {
  return {
    id: "p1",
    title: "Подборка שלום",
    description: "<i>описание</i>",
    channel: BOB,
    video_count: 2,
    thumbnail_url: null,
    visibility: null,
    can_manage: false,
    created_at: "2026-11-10T00:00:00Z",
    updated_at: "2026-11-10T00:00:00Z",
    ...extra,
  };
}

const OWN = { channel: ME.channel, visibility: "private", can_manage: true };

function item(n: number, extra: Record<string, unknown> = {}) {
  return {
    position: n,
    id: `v${n}`,
    title: `Видео ${n}`,
    channel: BOB,
    duration_seconds: 30,
    views_count: n,
    published_at: "2026-11-10T00:00:00Z",
    thumbnail_url: null,
    state: null,
    ...extra,
  };
}

function api(routes: MockRoutes = {}) {
  return mockApi({
    "GET /api/auth/me": () => ME,
    "GET /api/playlists/p1": () => playlist(),
    "GET /api/playlists/p1/videos": () => ({ items: [item(1), item(2)], next_cursor: null }),
    ...routes,
  });
}

function render(route = "/playlist/p1") {
  return renderApp(
    <Routes>
      <Route path="/playlist/:playlistId" element={<PlaylistPage />} />
      <Route path="/channel/:handle" element={<p>страница канала</p>} />
    </Routes>,
    { route },
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("changedPlaylistFields", () => {
  it("только изменённые поля", () => {
    const base = { title: "a", description: "b", visibility: "public" as const };
    expect(changedPlaylistFields(base, base)).toEqual({});
    expect(changedPlaylistFields(base, { ...base, description: "c" })).toEqual({ description: "c" });
  });
});

describe("PlaylistPage: просмотр", () => {
  it("название и описание — текстом с dir=auto; видео по порядку; без управления у чужого", async () => {
    api();
    const { container } = render();
    const title = await screen.findByRole("heading", { level: 1 });
    expect(title).toHaveAttribute("dir", "auto");
    expect(screen.getByText("<i>описание</i>")).toHaveAttribute("dir", "auto");
    expect(container.querySelector("i")).toBeNull();
    const rows = await screen.findAllByRole("listitem");
    expect(rows.map((r) => within(r).getByRole("heading").textContent)).toEqual(["Видео 1", "Видео 2"]);
    expect(screen.queryByRole("button", { name: "Изменить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Убрать/ })).not.toBeInTheDocument();
  });

  it.each([
    [404, "Not found"],
    [422, "Проверьте правильность полей: playlist_id"],
  ])("%i — «Плейлист не найден» без подробностей и «Повторить»", async (status, detail) => {
    api({ "GET /api/playlists/p1": () => json({ detail }, status) });
    render();
    expect(await screen.findByText("Плейлист не найден")).toBeInTheDocument();
    expect(screen.queryByText(detail)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
  });

  it("UUID в адресе — в нижнем регистре (ключ кэша совпадает с ответом API)", async () => {
    const get = vi.fn(() => playlist());
    api({ "GET /api/playlists/p1": get });
    render("/playlist/P1");
    await screen.findByRole("heading", { level: 1 });
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("пустой плейлист — подсказка владельцу", async () => {
    api({ "GET /api/playlists/p1": () => playlist(OWN), "GET /api/playlists/p1/videos": () => ({ items: [], next_cursor: null }) });
    render();
    expect(await screen.findByText("В плейлисте пока нет видео")).toBeInTheDocument();
    expect(screen.getByText(/кнопкой «Сохранить»/)).toBeInTheDocument();
  });
});

describe("PlaylistPage: владелец", () => {
  it("видимость и статусы видео видны владельцу", async () => {
    api({
      "GET /api/playlists/p1": () => playlist(OWN),
      "GET /api/playlists/p1/videos": () => ({
        items: [item(1, { state: { status: "processing", visibility: "public", moderation: "ok", moderation_reason: null, failure_reason: null } })],
        next_cursor: null,
      }),
    });
    render();
    expect(await screen.findByText("Только я")).toBeInTheDocument();
    expect(await screen.findByText("Обрабатывается")).toBeInTheDocument();
  });

  it("«Убрать» — DELETE элемента, список перезапрошен", async () => {
    let items = [item(1), item(2)];
    const del = vi.fn(() => ((items = items.filter((i) => i.id !== "v1")), new Response(null, { status: 204 })));
    api({
      "GET /api/playlists/p1": () => playlist(OWN),
      "GET /api/playlists/p1/videos": () => ({ items, next_cursor: null }),
      "DELETE /api/playlists/p1/videos/v1": del,
    });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Убрать из плейлиста: Видео 1" }));
    await waitFor(() => expect(screen.queryByText("Видео 1")).not.toBeInTheDocument());
    expect(del).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Видео 2")).toBeInTheDocument();
  });

  it("«Убрать» с ошибкой — сообщение, видео остаётся", async () => {
    api({
      "GET /api/playlists/p1": () => playlist(OWN),
      "DELETE /api/playlists/p1/videos/v1": () => json({ detail: "Плейлист сейчас изменяется" }, 409),
    });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Убрать из плейлиста: Видео 1" }));
    expect(await screen.findByText("Плейлист сейчас изменяется")).toBeInTheDocument();
    expect(screen.getByText("Видео 1")).toBeInTheDocument();
  });

  it("правка — PATCH только изменённых полей", async () => {
    let current = playlist(OWN);
    let body: unknown = null;
    const patch = vi.fn((_u: URL, init: RequestInit) => {
      body = JSON.parse(String(init.body));
      current = playlist({ ...OWN, visibility: "public" });
      return current;
    });
    api({ "GET /api/playlists/p1": () => current, "PATCH /api/playlists/p1": patch });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    expect(screen.getByLabelText("Название плейлиста")).toHaveFocus();
    fireEvent.click(screen.getByLabelText(/Открытый/));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(screen.queryByLabelText("Название плейлиста")).not.toBeInTheDocument());
    expect(patch).toHaveBeenCalledTimes(1);
    expect(body).toEqual({ visibility: "public" });
    expect(screen.getByText("Открытый")).toBeInTheDocument();
  });

  it("правка без изменений — без запроса, форма закрывается", async () => {
    const patch = vi.fn();
    api({ "GET /api/playlists/p1": () => playlist(OWN), "PATCH /api/playlists/p1": patch });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Изменить" }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(screen.queryByLabelText("Название плейлиста")).not.toBeInTheDocument());
    expect(patch).not.toHaveBeenCalled();
  });

  it("ошибка удаления — сообщение, остаёмся на странице", async () => {
    api({ "GET /api/playlists/p1": () => playlist(OWN), "DELETE /api/playlists/p1": () => json({ detail: "Плейлист сейчас изменяется" }, 409) });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Удалить плейлист" }));
    fireEvent.click(within(screen.getByRole("group", { name: "Удалить плейлист?" })).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Плейлист сейчас изменяется");
    expect(screen.getByTestId("location")).toHaveTextContent("/playlist/p1");
  });

  it("удаление — подтверждение (фокус на нём), DELETE, переход на свой канал без повторного GET", async () => {
    const del = vi.fn(() => new Response(null, { status: 204 }));
    const get = vi.fn(() => playlist(OWN));
    const getVideos = vi.fn(() => ({ items: [item(1)], next_cursor: null }));
    api({ "GET /api/playlists/p1": get, "GET /api/playlists/p1/videos": getVideos, "DELETE /api/playlists/p1": del });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Удалить плейлист" }));
    const confirm = within(screen.getByRole("group", { name: "Удалить плейлист?" })).getByRole("button", { name: "Удалить" });
    expect(confirm).toHaveFocus();
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    expect(await screen.findByText("страница канала")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/channel/alice?tab=playlists");
    await new Promise((r) => setTimeout(r, 50));
    // Удалённый плейлист и его состав не перезапрашиваются (иначе 404 и мелькание «не найден»).
    expect(get).toHaveBeenCalledTimes(1);
    expect(getVideos).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Плейлист не найден")).not.toBeInTheDocument();
  });
});
