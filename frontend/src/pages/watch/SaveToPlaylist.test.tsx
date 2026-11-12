import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, type MockRoutes } from "../../test/utils";
import { WatchPage } from "./WatchPage";

const READY = { status: "ready", visibility: "public", moderation: "ok", moderation_reason: null, failure_reason: null };

function card(own: boolean) {
  return {
    id: "v1",
    title: "Видео",
    description: "",
    channel: own ? ME.channel : { id: "c2", handle: "bob", title: "Боб" },
    duration_seconds: 20,
    views_count: 0,
    likes_count: 0,
    dislikes_count: 0,
    comments_count: 0,
    published_at: "2026-11-10T12:00:00Z",
    thumbnail_url: null,
    my_reaction: null,
    state: own ? READY : null,
  };
}

function playlist(n: number, contains: boolean, visibility = "public") {
  return { id: `p${n}`, title: `Плейлист ${n}`, video_count: 1, thumbnail_url: null, visibility, contains_video: contains, created_at: "2026-11-10T00:00:00Z" };
}

function api(routes: MockRoutes = {}, own = true) {
  return mockApi({
    "GET /api/auth/me": () => ME,
    "GET /api/videos/v1": () => card(own),
    "GET /api/videos/v1/stream": () => ({ video_id: "v1", sources: [], thumbnail_url: null, expires_in: 900 }),
    "GET /api/videos/v1/comments": () => ({ items: [], next_cursor: null }),
    "GET /api/channels/alice": () => ({ ...ME.channel, description: "", subscribers_count: 0, created_at: "2026-11-01T00:00:00Z", subscribed: null }),
    "GET /api/channels/bob": () => ({ id: "c2", handle: "bob", title: "Боб", description: "", subscribers_count: 0, created_at: "2026-11-01T00:00:00Z", subscribed: false }),
    "GET /api/me/playlists": () => ({ items: [playlist(1, true, "private"), playlist(2, false)], next_cursor: null }),
    ...routes,
  });
}

const render = () =>
  renderApp(
    <Routes>
      <Route path="/watch/:videoId" element={<WatchPage />} />
    </Routes>,
    { route: "/watch/v1" },
  );

async function openPanel() {
  fireEvent.click(await screen.findByRole("button", { name: "Сохранить в плейлист" }));
  return screen.findByRole("group", { name: "Сохранить в плейлист" });
}

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("SaveToPlaylist", () => {
  it("только на своём видео", async () => {
    api({}, false);
    render();
    await screen.findByRole("heading", { name: "Видео" });
    expect(screen.queryByRole("button", { name: "Сохранить в плейлист" })).not.toBeInTheDocument();
  });

  it("список своих плейлистов с отметкой по contains_video; запрос — с video_id", async () => {
    const list = vi.fn((url: URL) => {
      expect(url.searchParams.get("video_id")).toBe("v1");
      return { items: [playlist(1, true, "private"), playlist(2, false)], next_cursor: null };
    });
    api({ "GET /api/me/playlists": list });
    render();
    const panel = await openPanel();
    expect(await within(panel).findByLabelText(/Плейлист 1/)).toBeChecked();
    expect(within(panel).getByLabelText(/Плейлист 2/)).not.toBeChecked();
    expect(within(panel).getByText("Только я")).toBeInTheDocument();
  });

  it("отметить — PUT, снять — DELETE; галочка — по успеху", async () => {
    const put = vi.fn(() => json({ playlist_id: "p2", video_id: "v1", position: 1, added_at: "2026-11-12T00:00:00Z" }, 201));
    const del = vi.fn(() => new Response(null, { status: 204 }));
    api({ "PUT /api/playlists/p2/videos/v1": put, "DELETE /api/playlists/p1/videos/v1": del });
    render();
    const panel = await openPanel();
    fireEvent.click(await within(panel).findByLabelText(/Плейлист 2/));
    await waitFor(() => expect(within(panel).getByLabelText(/Плейлист 2/)).toBeChecked());
    expect(put).toHaveBeenCalledTimes(1);
    fireEvent.click(within(panel).getByLabelText(/Плейлист 1/));
    await waitFor(() => expect(within(panel).getByLabelText(/Плейлист 1/)).not.toBeChecked());
    expect(del).toHaveBeenCalledTimes(1);
  });

  it("лимит (409) — сообщение сервера, галочка не ставится", async () => {
    api({ "PUT /api/playlists/p2/videos/v1": () => json({ detail: "Достигнут лимит видео в плейлисте" }, 409) });
    render();
    const panel = await openPanel();
    fireEvent.click(await within(panel).findByLabelText(/Плейлист 2/));
    expect(await within(panel).findByText("Достигнут лимит видео в плейлисте")).toBeInTheDocument();
    expect(within(panel).getByLabelText(/Плейлист 2/)).not.toBeChecked();
  });

  it("новый плейлист — POST, затем PUT видео, список перезапрошен", async () => {
    let created = false;
    const post = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ title: "Свежий", description: "", visibility: "public" });
      created = true;
      return json(
        { id: "p3", title: "Свежий", description: "", channel: ME.channel, video_count: 0, thumbnail_url: null, visibility: "public", can_manage: true, created_at: "x", updated_at: "x" },
        201,
      );
    });
    const put = vi.fn(() => json({ playlist_id: "p3", video_id: "v1", position: 1, added_at: "x" }, 201));
    api({
      "POST /api/playlists": post,
      "PUT /api/playlists/p3/videos/v1": put,
      "GET /api/me/playlists": () => ({
        items: [...(created ? [{ ...playlist(3, true), title: "Свежий" }] : []), playlist(1, true, "private"), playlist(2, false)],
        next_cursor: null,
      }),
    });
    render();
    const panel = await openPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "+ Новый плейлист" }));
    fireEvent.change(within(panel).getByLabelText("Название плейлиста"), { target: { value: "Свежий" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Создать и добавить" }));
    expect(await within(panel).findByLabelText(/Свежий/)).toBeChecked();
    expect(post).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("создан, но видео не добавлено — форма закрыта (повтор не создаст дубль), сообщение", async () => {
    const post = vi.fn(() =>
      json({ id: "p3", title: "Свежий", description: "", channel: ME.channel, video_count: 0, thumbnail_url: null, visibility: "public", can_manage: true, created_at: "x", updated_at: "x" }, 201),
    );
    api({ "POST /api/playlists": post, "PUT /api/playlists/p3/videos/v1": () => json({ detail: "Видео не найдено" }, 404) });
    render();
    const panel = await openPanel();
    fireEvent.click(within(panel).getByRole("button", { name: "+ Новый плейлист" }));
    fireEvent.change(within(panel).getByLabelText("Название плейлиста"), { target: { value: "Свежий" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Создать и добавить" }));
    expect(await within(panel).findByText(/Плейлист «Свежий» создан, но видео не добавлено/)).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Создать и добавить" })).not.toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("плейлист удалён (404 при отметке) — список перезапрошен", async () => {
    let calls = 0;
    api({
      "GET /api/me/playlists": () => (++calls === 1 ? { items: [playlist(1, true), playlist(2, false)], next_cursor: null } : { items: [playlist(1, true)], next_cursor: null }),
      "PUT /api/playlists/p2/videos/v1": () => json({ detail: "Плейлист не найден" }, 404),
    });
    render();
    const panel = await openPanel();
    fireEvent.click(await within(panel).findByLabelText(/Плейлист 2/));
    await waitFor(() => expect(within(panel).queryByLabelText(/Плейлист 2/)).not.toBeInTheDocument());
    expect(within(panel).getByText("Плейлист не найден")).toBeInTheDocument();
  });

  it("во время запроса галочка не блокируется через disabled (фокус не теряется)", async () => {
    let release: () => void = () => undefined;
    api({
      "PUT /api/playlists/p2/videos/v1": async () => {
        await new Promise<void>((r) => (release = r));
        return json({ playlist_id: "p2", video_id: "v1", position: 1, added_at: "x" }, 201);
      },
    });
    render();
    const panel = await openPanel();
    const box = await within(panel).findByLabelText(/Плейлист 2/);
    box.focus();
    fireEvent.click(box);
    await waitFor(() => expect(box).toHaveAttribute("aria-disabled", "true"));
    expect(box).not.toBeDisabled();
    expect(box).toHaveFocus();
    release();
    await waitFor(() => expect(box).toBeChecked());
  });
});
