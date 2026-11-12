import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, mockApi, renderApp } from "../test/utils";
import { MAX_SOURCE_RETRIES, RETRY_DELAY_MS, VideoPlayer } from "./VideoPlayer";

/** Управляемый <video>: jsdom не воспроизводит медиа — позицию, паузу и ошибку задаёт тест. */
interface FakeMedia {
  currentTime: number;
  paused: boolean;
  duration: number;
  error: { code: number } | null;
}

function fake(video: HTMLVideoElement): FakeMedia {
  const state: FakeMedia = { currentTime: 0, paused: true, duration: 60, error: null };
  for (const key of Object.keys(state) as (keyof FakeMedia)[]) {
    Object.defineProperty(video, key, {
      configurable: true,
      get: () => state[key],
      set: (v) => Object.assign(state, { [key]: v }),
    });
  }
  return state;
}

let streamCalls = 0;
let playSpy: ReturnType<typeof vi.fn>;
let loadSpy: ReturnType<typeof vi.fn>;

function streamResponse(n: number) {
  return {
    video_id: "v1",
    sources: [
      { quality: "720p", height: 720, mime: "video/mp4", size_bytes: 10, url: `/yt-media/v1/720.mp4?sig=${n}` },
      { quality: "360p", height: 360, mime: "video/mp4", size_bytes: 5, url: `/yt-media/v1/360.mp4?sig=${n}` },
    ],
    thumbnail_url: "/yt-media/v1/thumb.jpg?sig=1",
    expires_in: 900,
  };
}

beforeEach(() => {
  streamCalls = 0;
  playSpy = vi.fn(() => Promise.resolve());
  loadSpy = vi.fn();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(playSpy as never);
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(loadSpy as never);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function api(stream: () => Response | object = () => streamResponse(++streamCalls)) {
  return mockApi({
    "GET /api/auth/me": () => json({ detail: "Требуется вход" }, 401),
    "GET /api/videos/v1/stream": stream,
  });
}

async function renderPlayer(props: Partial<Parameters<typeof VideoPlayer>[0]> = {}) {
  renderApp(<VideoPlayer videoId="v1" durationSeconds={60} {...props} />);
  const video = (await screen.findByTestId("video")) as HTMLVideoElement;
  await waitFor(() => expect(video.getAttribute("src")).toContain("sig=1"));
  return { video, media: fake(video) };
}

function fail(video: HTMLVideoElement, media: FakeMedia, code = 2) {
  media.error = { code };
  fireEvent.error(video);
}

describe("VideoPlayer: перезапрос ссылки", () => {
  it("на ошибку источника заново запрашивает /stream и продолжает с той же позиции", async () => {
    api();
    const { video, media } = await renderPlayer();
    media.currentTime = 42.5;
    media.paused = false;

    fail(video, media);
    await waitFor(() => expect(video.getAttribute("src")).toContain("sig=2"));
    expect(streamCalls).toBe(2);

    // Новый источник загрузился: позиция восстановлена, воспроизведение продолжено.
    media.currentTime = 0;
    media.paused = true;
    media.error = null;
    fireEvent.loadedMetadata(video);
    expect(media.currentTime).toBe(42.5);
    expect(playSpy).toHaveBeenCalledTimes(1);
  });

  it("на паузе после восстановления не запускает воспроизведение", async () => {
    api();
    const { video, media } = await renderPlayer();
    media.currentTime = 10;
    media.paused = true;
    fail(video, media);
    await waitFor(() => expect(video.getAttribute("src")).toContain("sig=2"));
    media.currentTime = 0;
    fireEvent.loadedMetadata(video);
    expect(media.currentTime).toBe(10);
    expect(playSpy).not.toHaveBeenCalled();
  });

  it("если перезапрос вернул ту же ссылку — перезагружает источник явно", async () => {
    api(() => {
      streamCalls++;
      return streamResponse(1);
    });
    const { video, media } = await renderPlayer();
    fail(video, media);
    await waitFor(() => expect(streamCalls).toBe(2));
    await waitFor(() => expect(loadSpy).toHaveBeenCalledTimes(1));
  });

  it("прерывание загрузки (MEDIA_ERR_ABORTED) — не сбой", async () => {
    api();
    const { video, media } = await renderPlayer();
    fail(video, media, 1);
    await act(async () => undefined);
    expect(streamCalls).toBe(1);
  });

  it("второй сбой подряд — перезапрос с паузой, не сразу", async () => {
    api();
    const { video, media } = await renderPlayer();
    fail(video, media);
    await waitFor(() => expect(streamCalls).toBe(2));
    fireEvent.loadedMetadata(video);
    fail(video, media);
    await new Promise((r) => setTimeout(r, RETRY_DELAY_MS / 2));
    expect(streamCalls).toBe(2);
    await waitFor(() => expect(streamCalls).toBe(3), { timeout: RETRY_DELAY_MS * 2 });
  });

  it("повторные error во время перезапроса не запускают второй запрос", async () => {
    let release: () => void = () => undefined;
    let calls = 0;
    api(async () => {
      calls++;
      if (calls === 2) await new Promise<void>((r) => (release = r));
      return streamResponse(calls);
    });
    const { video, media } = await renderPlayer();
    fail(video, media);
    await waitFor(() => expect(calls).toBe(2));
    fail(video, media);
    fail(video, media);
    release();
    await waitFor(() => expect(video.getAttribute("src")).toContain("sig=2"));
    // Дольше паузы перед повтором: лишний перезапрос успел бы уйти.
    await new Promise((r) => setTimeout(r, RETRY_DELAY_MS * 2.5));
    expect(calls).toBe(2);
  });

  it(`после ${MAX_SOURCE_RETRIES} повторов без продвижения — сообщение, без цикла запросов`, async () => {
    api();
    const { video, media } = await renderPlayer();
    media.currentTime = 30;
    for (let i = 1; i <= MAX_SOURCE_RETRIES; i++) {
      fail(video, media);
      await waitFor(() => expect(video.getAttribute("src")).toContain(`sig=${i + 1}`), { timeout: 3000 });
      fireEvent.loadedMetadata(video);
    }
    fail(video, media);
    expect(await screen.findByText("Не удалось воспроизвести видео")).toBeInTheDocument();
    expect(streamCalls).toBe(MAX_SOURCE_RETRIES + 1);

    // Смена качества при сообщении — новая попытка, сообщение убирается.
    fireEvent.change(screen.getByLabelText(/Качество/), { target: { value: "360p" } });
    expect(screen.queryByText("Не удалось воспроизвести видео")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Качество/), { target: { value: "720p" } });
    for (let i = 0; i < MAX_SOURCE_RETRIES; i++) {
      fail(video, media);
      await waitFor(() => expect(streamCalls).toBe(MAX_SOURCE_RETRIES + 2 + i), { timeout: 3000 });
      fireEvent.loadedMetadata(video);
    }
    fail(video, media);
    expect(await screen.findByText("Не удалось воспроизвести видео")).toBeInTheDocument();
    const before = streamCalls;

    // «Повторить» — снова запрос ссылки.
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() => expect(streamCalls).toBe(before + 1));
    expect(screen.queryByText("Не удалось воспроизвести видео")).not.toBeInTheDocument();
  }, 15_000);

  it("продвижение воспроизведения после сбоя сбрасывает счётчик повторов", async () => {
    api();
    const { video, media } = await renderPlayer();
    for (let round = 0; round < MAX_SOURCE_RETRIES + 2; round++) {
      media.currentTime = round * 100;
      fail(video, media);
      await waitFor(() => expect(streamCalls).toBe(round + 2));
      fireEvent.loadedMetadata(video);
      media.currentTime = round * 100 + 6; // проиграли 6 с после восстановления
      fireEvent.timeUpdate(video);
    }
    expect(screen.queryByText("Не удалось воспроизвести видео")).not.toBeInTheDocument();
  });

  it("если доступ отозван (/stream → 404) — «Видео недоступно»", async () => {
    let calls = 0;
    api(() => (++calls === 1 ? streamResponse(1) : json({ detail: "Видео не найдено" }, 404)));
    const { video, media } = await renderPlayer();
    fail(video, media);
    expect(await screen.findByText("Видео недоступно")).toBeInTheDocument();
  });
});

describe("VideoPlayer: ссылки из ответа", () => {
  it.each([["javascript", "alert(1)"].join(":"), "//evil.example/x.mp4", "https://evil.example/x.mp4", "/\\evil/x", "/yt-media/x\t.mp4"])(
    "враждебная ссылка %j не ставится в src и poster — сообщение",
    async (url) => {
      api(() => ({ ...streamResponse(1), sources: [{ ...streamResponse(1).sources[0], url }], thumbnail_url: url }));
      renderApp(<VideoPlayer videoId="v1" durationSeconds={60} />);
      expect(await screen.findByText("Не удалось загрузить видео")).toBeInTheDocument();
      expect(document.querySelector("video")).toBeNull();
    },
  );

  it("враждебный poster не ставится, видео играет", async () => {
    api(() => ({ ...streamResponse(1), thumbnail_url: "//evil.example/p.jpg" }));
    const { video } = await renderPlayer();
    expect(video.hasAttribute("poster")).toBe(false);
  });
});

describe("VideoPlayer: качество", () => {
  it("смена качества сохраняет позицию и воспроизведение", async () => {
    api();
    const { video, media } = await renderPlayer();
    expect(video.getAttribute("src")).toContain("720.mp4");
    media.currentTime = 12;
    media.paused = false;
    fireEvent.change(screen.getByLabelText(/Качество/), { target: { value: "360p" } });
    expect(video.getAttribute("src")).toContain("360.mp4");
    media.currentTime = 0;
    fireEvent.loadedMetadata(video);
    expect(media.currentTime).toBe(12);
    expect(playSpy).toHaveBeenCalledTimes(1);
  });
});

describe("VideoPlayer: просмотр", () => {
  function playTo(video: HTMLVideoElement, media: FakeMedia, from: number, to: number) {
    for (let t = from; t <= to; t += 0.5) {
      media.currentTime = t;
      fireEvent.timeUpdate(video);
    }
  }

  it("засчитывается один раз после min(30 с, половины длительности)", async () => {
    api();
    const onWatched = vi.fn();
    const { video, media } = await renderPlayer({ durationSeconds: 20, onWatched });
    playTo(video, media, 0, 9.5);
    expect(onWatched).not.toHaveBeenCalled();
    playTo(video, media, 10, 10.5);
    expect(onWatched).toHaveBeenCalledTimes(1);
    playTo(video, media, 11, 20);
    expect(onWatched).toHaveBeenCalledTimes(1);
  });

  it("порог не больше 30 с для длинного видео", async () => {
    api();
    const onWatched = vi.fn();
    const { video, media } = await renderPlayer({ durationSeconds: 3600, onWatched });
    playTo(video, media, 0, 29.5);
    expect(onWatched).not.toHaveBeenCalled();
    playTo(video, media, 30, 31);
    expect(onWatched).toHaveBeenCalledTimes(1);
  });

  it("небольшая перемотка (меньше шага timeupdate) не засчитывается", async () => {
    api();
    const onWatched = vi.fn();
    const { video, media } = await renderPlayer({ durationSeconds: 20, onWatched });
    playTo(video, media, 0, 9.5);
    for (let t = 10; t <= 30; t += 1) {
      // Каждую секунду — перемотка на 1 с вперёд: воспроизведения нет, только скачки.
      fireEvent.seeking(video);
      media.currentTime = t;
      fireEvent.timeUpdate(video);
    }
    expect(onWatched).not.toHaveBeenCalled();
  });

  it("перемотка вперёд не засчитывается", async () => {
    api();
    const onWatched = vi.fn();
    const { video, media } = await renderPlayer({ durationSeconds: 20, onWatched });
    playTo(video, media, 0, 1);
    fireEvent.seeking(video);
    media.currentTime = 19;
    fireEvent.timeUpdate(video);
    expect(onWatched).not.toHaveBeenCalled();
  });
});
