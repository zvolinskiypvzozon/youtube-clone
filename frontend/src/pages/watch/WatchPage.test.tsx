import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, type Handler, type MockRoutes } from "../../test/utils";
import { PROCESSING_POLL_MS, WatchPage } from "./WatchPage";

const BOB_CHANNEL = { id: "c2", handle: "bob", title: "Канал Боба" };

function card(overrides: Record<string, unknown> = {}) {
  return {
    id: "v1",
    title: "Видео про котов",
    description: "Строка 1\nСтрока 2",
    channel: BOB_CHANNEL,
    duration_seconds: 20,
    views_count: 7,
    likes_count: 5,
    dislikes_count: 1,
    comments_count: 0,
    published_at: "2026-11-10T12:00:00Z",
    thumbnail_url: null,
    my_reaction: null,
    state: null,
    ...overrides,
  };
}

const CHANNEL = {
  ...BOB_CHANNEL,
  description: "",
  subscribers_count: 3,
  created_at: "2026-11-01T00:00:00Z",
  subscribed: false,
};

const STREAM = {
  video_id: "v1",
  sources: [{ quality: "720p", height: 720, mime: "video/mp4", size_bytes: 1, url: "/yt-media/v1/720.mp4?s=1" }],
  thumbnail_url: null,
  expires_in: 900,
};

const anon: Handler = () => json({ detail: "Требуется вход" }, 401);

function api(routes: MockRoutes = {}, { user = ME as object | null } = {}) {
  return mockApi({
    "GET /api/auth/me": user ? () => user : anon,
    "GET /api/videos/v1": () => card(),
    "GET /api/videos/v1/stream": () => STREAM,
    "GET /api/channels/bob": () => CHANNEL,
    ...routes,
  });
}

function render() {
  return renderApp(
    <Routes>
      <Route path="/watch/:videoId" element={<WatchPage />} />
      <Route path="/login" element={<p>Страница входа</p>} />
    </Routes>,
    { route: "/watch/v1" },
  );
}

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("WatchPage: доступ", () => {
  it.each([
    ["нет видео", "Видео не найдено"],
    ["приватное без доступа", "Видео не найдено или нет доступа"],
  ])("404 (%s) — одинаковая страница «Видео недоступно» без подробностей", async (_case, detail) => {
    api({ "GET /api/videos/v1": () => json({ detail }, 404) });
    render();
    expect(await screen.findByText("Видео недоступно")).toBeInTheDocument();
    expect(screen.queryByText(detail)).not.toBeInTheDocument();
    expect(screen.queryByTestId("video")).not.toBeInTheDocument();
  });

  it("другая ошибка — сообщение и «Повторить»", async () => {
    let calls = 0;
    api({ "GET /api/videos/v1": () => (++calls === 1 ? json({ detail: "x" }, 500) : card()) });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));
    expect(await screen.findByRole("heading", { name: "Видео про котов" })).toBeInTheDocument();
  });
});

describe("WatchPage: пользовательский текст", () => {
  it("название, описание и канал выводятся текстом с dir=auto", async () => {
    const html = '<img src=x onerror="alert(1)">';
    api({
      "GET /api/videos/v1": () =>
        card({ title: html, description: `<b>жирный</b>`, channel: { ...BOB_CHANNEL, title: "שלום" } }),
    });
    const { container } = render();
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent(html);
    expect(heading).toHaveAttribute("dir", "auto");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("<b>жирный</b>")).toHaveAttribute("dir", "auto");
    expect(screen.getAllByText("שלום")[0]).toHaveAttribute("dir", "auto");
  });
});

describe("WatchPage: реакции", () => {
  it("лайк — PUT, счётчики из ответа сервера", async () => {
    const put = vi.fn(() => ({ my_reaction: "like", likes_count: 10, dislikes_count: 1 }));
    const fetchMock = api({ "PUT /api/videos/v1/reaction": put });
    render();
    const like = await screen.findByRole("button", { name: /Нравится/ });
    await waitFor(() => expect(like).toBeEnabled());
    fireEvent.click(like);
    await waitFor(() => expect(screen.getByRole("button", { name: "Нравится: 10" })).toHaveAttribute("aria-pressed", "true"));
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ kind: "like" });
  });

  it("повторный клик по своей реакции — DELETE", async () => {
    const del = vi.fn(() => ({ my_reaction: null, likes_count: 4, dislikes_count: 1 }));
    api({ "GET /api/videos/v1": () => card({ my_reaction: "like" }), "DELETE /api/videos/v1/reaction": del });
    render();
    const like = await screen.findByRole("button", { name: /Нравится/ });
    expect(like).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(like).toBeEnabled());
    fireEvent.click(like);
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Нравится: 4" })).toHaveAttribute("aria-pressed", "false"));
  });

  it("дизлайк при лайке — PUT dislike", async () => {
    const put = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ kind: "dislike" });
      return { my_reaction: "dislike", likes_count: 4, dislikes_count: 2 };
    });
    api({ "GET /api/videos/v1": () => card({ my_reaction: "like" }), "PUT /api/videos/v1/reaction": put });
    render();
    const dislike = await screen.findByRole("button", { name: /Не нравится/ });
    await waitFor(() => expect(dislike).toBeEnabled());
    fireEvent.click(dislike);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });

  it("аноним по клику уходит на вход", async () => {
    const put = vi.fn();
    api({ "PUT /api/videos/v1/reaction": put }, { user: null });
    render();
    const like = await screen.findByRole("button", { name: /Нравится/ });
    await waitFor(() => expect(like).toBeEnabled());
    fireEvent.click(like);
    expect(await screen.findByText("Страница входа")).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
  });

  it("ошибка сервера показывается текстом, счётчики не меняются", async () => {
    api({ "PUT /api/videos/v1/reaction": () => json({ detail: "Видео ещё не обработано" }, 409) });
    render();
    const like = await screen.findByRole("button", { name: /Нравится/ });
    await waitFor(() => expect(like).toBeEnabled());
    fireEvent.click(like);
    expect(await screen.findByText("Видео ещё не обработано")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Нравится: 5" })).toHaveAttribute("aria-pressed", "false");
  });
});

describe("WatchPage: подписка", () => {
  it("подписка — PUT, состояние и счётчик из ответа", async () => {
    const put = vi.fn(() => ({ subscribed: true, subscribers_count: 4 }));
    api({ "PUT /api/channels/bob/subscription": put });
    render();
    const button = await screen.findByRole("button", { name: "Подписаться" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "Вы подписаны" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("4 подписчика")).toBeInTheDocument();
  });

  it("отписка — DELETE", async () => {
    const del = vi.fn(() => ({ subscribed: false, subscribers_count: 2 }));
    api({ "GET /api/channels/bob": () => ({ ...CHANNEL, subscribed: true }), "DELETE /api/channels/bob/subscription": del });
    render();
    const button = await screen.findByRole("button", { name: "Вы подписаны" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("button", { name: "Подписаться" })).toBeInTheDocument();
    expect(del).toHaveBeenCalledTimes(1);
  });

  it("на своём видео кнопки подписки и жалобы нет", async () => {
    api({
      "GET /api/videos/v1": () =>
        card({
          channel: ME.channel,
          state: { status: "ready", visibility: "public", moderation: "ok", moderation_reason: null, failure_reason: null },
        }),
      "GET /api/channels/alice": () => ({ ...CHANNEL, ...ME.channel, subscribed: false }),
    });
    render();
    await screen.findByText(/подписчик/);
    expect(screen.queryByRole("button", { name: "Подписаться" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Пожаловаться" })).not.toBeInTheDocument();
  });

  it("канал забаненного (404) — без блока подписки, видео показывается", async () => {
    api({ "GET /api/channels/bob": () => json({ detail: "Канал не найден" }, 404) });
    render();
    expect(await screen.findByRole("heading", { name: "Видео про котов" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Подписаться" })).not.toBeInTheDocument());
  });
});

describe("WatchPage: состояние для владельца и staff", () => {
  const state = (s: Record<string, unknown>) => ({
    status: "ready",
    visibility: "public",
    moderation: "ok",
    moderation_reason: null,
    failure_reason: null,
    ...s,
  });

  it("обрабатывается — статус вместо плеера, без реакций", async () => {
    api({ "GET /api/videos/v1": () => card({ channel: ME.channel, state: state({ status: "processing" }) }) });
    render();
    expect(await screen.findByText(/Видео обрабатывается\. Страница обновится/)).toBeInTheDocument();
    expect(screen.queryByTestId("video")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Нравится/ })).not.toBeInTheDocument();
  });

  it("скрыто модерацией — причина текстом, кнопки жалобы нет", async () => {
    api({
      "GET /api/videos/v1": () =>
        card({ state: state({ moderation: "hidden", moderation_reason: "<i>спам</i>" }) }),
    });
    const { container } = render();
    const banner = await screen.findByText(/Видео скрыто модерацией/);
    expect(within(banner).getByText("<i>спам</i>")).toHaveAttribute("dir", "auto");
    expect(container.querySelector("i")).toBeNull();
    expect(screen.queryByRole("button", { name: "Пожаловаться" })).not.toBeInTheDocument();
  });
});

describe("WatchPage: просмотр", () => {
  it("после порога воспроизведения — один POST /view", async () => {
    const view = vi.fn(() => new Response(null, { status: 204 }));
    api({ "POST /api/videos/v1/view": view });
    render();
    const video = (await screen.findByTestId("video")) as HTMLVideoElement;
    let t = 0;
    Object.defineProperty(video, "currentTime", { configurable: true, get: () => t, set: (v) => (t = v) });
    for (t = 0; t <= 15; t += 0.5) fireEvent.timeUpdate(video);
    await waitFor(() => expect(view).toHaveBeenCalledTimes(1));
    for (; t <= 20; t += 0.5) fireEvent.timeUpdate(video);
    expect(view).toHaveBeenCalledTimes(1);
  });
});

describe("WatchPage: жалоба", () => {
  it("категория и пояснение уходят на сервер, затем подтверждение", async () => {
    const post = vi.fn(() => json({ id: "r1", category: "spam", status: "open" }, 201));
    const fetchMock = api({ "POST /api/videos/v1/reports": post });
    render();
    const open = await screen.findByRole("button", { name: "Пожаловаться" });
    await waitFor(() => expect(open).toBeEnabled());
    fireEvent.click(open);
    const submit = screen.getByRole("button", { name: "Отправить" });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Спам или обман"));
    fireEvent.change(screen.getByLabelText(/Пояснение/), { target: { value: "  реклама  " } });
    fireEvent.click(submit);
    expect(await screen.findByText(/жалоба отправлена/)).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ category: "spam", comment: "реклама" });
  });

  it("повторная жалоба (409) — сообщение сервера", async () => {
    api({ "POST /api/videos/v1/reports": () => json({ detail: "Жалоба уже отправлена" }, 409) });
    render();
    const open = await screen.findByRole("button", { name: "Пожаловаться" });
    await waitFor(() => expect(open).toBeEnabled());
    fireEvent.click(open);
    fireEvent.click(screen.getByLabelText("Другое"));
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));
    expect(await screen.findByText("Жалоба уже отправлена")).toBeInTheDocument();
  });

  it("аноним по клику уходит на вход", async () => {
    api({}, { user: null });
    render();
    const open = await screen.findByRole("button", { name: "Пожаловаться" });
    await waitFor(() => expect(open).toBeEnabled());
    fireEvent.click(open);
    expect(await screen.findByText("Страница входа")).toBeInTheDocument();
  });
});

describe("WatchPage: ревью шага 1", () => {
  const st = (status: string) => ({
    status,
    visibility: "public",
    moderation: "ok",
    moderation_reason: null,
    failure_reason: null,
  });

  it("processing → ready: опрос карточки сменяет статус на плеер и останавливается", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let calls = 0;
      api({ "GET /api/videos/v1": () => card({ channel: ME.channel, state: st(++calls < 2 ? "processing" : "ready") }) });
      render();
      expect(await screen.findByText(/Видео обрабатывается\. Страница обновится/)).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(PROCESSING_POLL_MS);
      expect(await screen.findByTestId("video")).toBeInTheDocument();
      const after = calls;
      await vi.advanceTimersByTimeAsync(PROCESSING_POLL_MS * 3);
      expect(calls).toBe(after);
    } finally {
      vi.useRealTimers();
    }
  });

  it("брошенная загрузка (uploading) не опрашивается", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const get = vi.fn(() => card({ channel: ME.channel, state: st("uploading") }));
      api({ "GET /api/videos/v1": get });
      render();
      await screen.findByRole("heading", { name: "Видео про котов" });
      await vi.advanceTimersByTimeAsync(PROCESSING_POLL_MS * 3);
      expect(get).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("аноним жмёт «Подписаться» — уходит на вход", async () => {
    const put = vi.fn();
    api({ "PUT /api/channels/bob/subscription": put }, { user: null });
    render();
    const button = await screen.findByRole("button", { name: "Подписаться" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByText("Страница входа")).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
  });

  it("401 посреди просмотра: плеер не размонтируется, карточка перезапрашивается", async () => {
    let cardCalls = 0;
    let release: () => void = () => undefined;
    const streamGet = vi.fn(() => STREAM);
    api({
      // Ответ перезапроса — по команде теста: проверяем плеер, пока карточка перезапрашивается.
      "GET /api/videos/v1": async () => {
        if (++cardCalls === 1) return card({ my_reaction: "like" });
        await new Promise<void>((r) => (release = r));
        return card({ my_reaction: null });
      },
      "GET /api/videos/v1/stream": streamGet,
      "PUT /api/videos/v1/reaction": () => json({ detail: "Требуется вход" }, 401),
    });
    render();
    const video = await screen.findByTestId("video");
    const dislike = await screen.findByRole("button", { name: /Не нравится/ });
    await waitFor(() => expect(dislike).toBeEnabled());
    fireEvent.click(dislike);
    await waitFor(() => expect(cardCalls).toBe(2));
    await act(async () => undefined);
    expect(screen.getByTestId("video")).toBe(video);
    release();
    // my_reaction прежней сессии заменён ответом под новой (анонимной).
    await waitFor(() => expect(screen.getByRole("button", { name: /^Нравится/ })).toHaveAttribute("aria-pressed", "false"));
    expect(cardCalls).toBe(2);
    expect(screen.getByTestId("video")).toBe(video);
    expect(streamGet).toHaveBeenCalledTimes(1);
  });

  it("после смены сессии карточка 404 — «Видео недоступно» вместо прежних данных", async () => {
    let cardCalls = 0;
    api({
      "GET /api/videos/v1": () => (++cardCalls === 1 ? card() : json({ detail: "Видео не найдено" }, 404)),
      "PUT /api/videos/v1/reaction": () => json({ detail: "Требуется вход" }, 401),
    });
    render();
    const like = await screen.findByRole("button", { name: /^Нравится/ });
    await waitFor(() => expect(like).toBeEnabled());
    fireEvent.click(like);
    expect(await screen.findByText("Видео недоступно")).toBeInTheDocument();
    expect(screen.queryByTestId("video")).not.toBeInTheDocument();
  });

  it("«Ещё» — только для длинного описания, с aria-expanded", async () => {
    api({ "GET /api/videos/v1": () => card({ description: "a\nb\nc\nd\ne" }) });
    render();
    const more = await screen.findByRole("button", { name: "Ещё" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(screen.getByRole("button", { name: "Свернуть" })).toHaveAttribute("aria-expanded", "true");
  });

  it("короткое описание — без «Ещё»", async () => {
    api({ "GET /api/videos/v1": () => card({ description: "коротко" }) });
    render();
    await screen.findByText("коротко");
    expect(screen.queryByRole("button", { name: "Ещё" })).not.toBeInTheDocument();
  });

  it("жалоба: фокус на форму при открытии и обратно на кнопку после «Отмены»", async () => {
    api();
    render();
    const open = await screen.findByRole("button", { name: "Пожаловаться" });
    await waitFor(() => expect(open).toBeEnabled());
    fireEvent.click(open);
    expect(screen.getByLabelText("Спам или обман")).toHaveFocus();
    expect(screen.getByLabelText(/Пояснение/)).toHaveAttribute("aria-describedby");
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(screen.getByRole("button", { name: "Пожаловаться" })).toHaveFocus();
  });
});
