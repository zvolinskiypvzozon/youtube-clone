import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, type Handler } from "../../test/utils";
import { titleFromFile, UploadPage, validateFile } from "./UploadPage";

/** XHR для частей: по умолчанию отвечает 200 с ETag; hold — не отвечает (отмена). */
class FakeXhr {
  static all: FakeXhr[] = [];
  static mode: "ok" | "hold" | "403" = "ok";
  url = "";
  body: Blob | null = null;
  status = 0;
  aborted = false;
  upload: { onprogress: ((e: { loaded: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  open(_m: string, url: string) {
    this.url = url;
  }
  send(body: Blob) {
    this.body = body;
    FakeXhr.all.push(this);
    if (FakeXhr.mode === "hold") return;
    setTimeout(() => {
      this.upload.onprogress?.({ loaded: body.size });
      this.status = FakeXhr.mode === "403" ? 403 : 200;
      this.onload?.();
    }, 0);
  }
  getResponseHeader(name: string) {
    return name === "ETag" && this.status === 200 ? `"etag-${new URL(this.url, "http://x").searchParams.get("n")}"` : null;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
}

function videoFile(name = "Мой ролик.mp4", bytes = 10, type = "video/mp4") {
  return new File([new Uint8Array(bytes)], name, { type });
}

const STARTED = {
  video_id: "v9",
  part_size: 4,
  expires_in: 3600,
  parts: [
    { number: 1, size: 4, url: "/yt-raw/raw/v9/source?n=1&X-Amz-Signature=a" },
    { number: 2, size: 4, url: "/yt-raw/raw/v9/source?n=2&X-Amz-Signature=b" },
    { number: 3, size: 2, url: "/yt-raw/raw/v9/source?n=3&X-Amz-Signature=c" },
  ],
};

function api(routes: Record<string, Handler> = {}) {
  return mockApi({
    "GET /api/auth/me": () => ME,
    "POST /api/uploads": () => json(STARTED, 201),
    "POST /api/uploads/v9/complete": () => ({ video_id: "v9", status: "processing" }),
    "DELETE /api/uploads/v9": () => new Response(null, { status: 204 }),
    ...routes,
  });
}

function Leave() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/elsewhere")}>
      уйти
    </button>
  );
}

function render() {
  return renderApp(
    <>
      <Routes>
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/elsewhere" element={<p>другая страница</p>} />
      </Routes>
      <Leave />
    </>,
    { route: "/upload" },
  );
}

function pick(file: File) {
  fireEvent.change(screen.getByLabelText("Файл"), { target: { files: [file] } });
}

const calls = (fetchMock: ReturnType<typeof api>, key: string) =>
  fetchMock.mock.calls.filter(([url, init]) => `${init?.method ?? "GET"} ${new URL(String(url), "http://x").pathname}` === key);

beforeEach(() => {
  FakeXhr.all = [];
  FakeXhr.mode = "ok";
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("validateFile / titleFromFile", () => {
  it("видео, не пустой, не больше 2 ГБ", () => {
    expect(validateFile(null)).toMatch(/Выберите/);
    expect(validateFile(videoFile("a.txt", 3, "text/plain"))).toMatch(/не видеофайл/);
    expect(validateFile(videoFile("a.mp4", 0))).toMatch(/пустой/);
    const big = videoFile();
    Object.defineProperty(big, "size", { value: 2 * 1024 ** 3 + 1 });
    expect(validateFile(big)).toMatch(/2 ГБ/);
    const max = videoFile();
    Object.defineProperty(max, "size", { value: 2 * 1024 ** 3 });
    expect(validateFile(max)).toBeNull();
  });

  it("название — имя файла без расширения, до 100 символов", () => {
    expect(titleFromFile("Мой ролик.final.mp4")).toBe("Мой ролик.final");
    expect(titleFromFile(`${"я".repeat(150)}.mov`)).toHaveLength(100);
  });
});

describe("UploadPage", () => {
  it("полный путь: старт → части срезами по 3 → complete с ETag → «Видео загружено»", async () => {
    const fetchMock = api();
    render();
    pick(videoFile());
    expect(screen.getByLabelText("Название")).toHaveValue("Мой ролик");
    fireEvent.change(screen.getByLabelText("Описание"), { target: { value: "описание" } });
    fireEvent.click(screen.getByLabelText(/Доступ по ссылке/));
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));

    expect(await screen.findByRole("heading", { name: "Видео загружено" })).toBeInTheDocument();
    const [start] = calls(fetchMock, "POST /api/uploads");
    expect(JSON.parse(String(start[1]?.body))).toEqual({
      title: "Мой ролик",
      description: "описание",
      visibility: "unlisted",
      content_type: "video/mp4",
      size_bytes: 10,
    });
    expect(FakeXhr.all.map((x) => [x.url, x.body?.size])).toEqual(STARTED.parts.map((p) => [p.url, p.size]));
    const [complete] = calls(fetchMock, "POST /api/uploads/v9/complete");
    expect(JSON.parse(String(complete[1]?.body))).toEqual({
      parts: [
        { number: 1, etag: '"etag-1"' },
        { number: 2, etag: '"etag-2"' },
        { number: 3, etag: '"etag-3"' },
      ],
    });
    expect(screen.getByRole("link", { name: "Открыть видео" })).toHaveAttribute("href", "/watch/v9");
    expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(0);
  });

  it("не видеофайл — ошибка у поля, запросов нет", async () => {
    const fetchMock = api();
    render();
    pick(videoFile("doc.pdf", 5, "application/pdf"));
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText(/не видеофайл/)).toBeInTheDocument();
    expect(calls(fetchMock, "POST /api/uploads")).toHaveLength(0);
  });

  it("без названия — ошибка у поля", async () => {
    const fetchMock = api();
    render();
    pick(videoFile(".mp4"));
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "  " } });
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText("Введите название")).toBeInTheDocument();
    expect(calls(fetchMock, "POST /api/uploads")).toHaveLength(0);
  });

  it("409 «3 в работе» — сообщение сервера, части не отправляются", async () => {
    api({ "POST /api/uploads": () => json({ detail: "Слишком много незавершённых загрузок" }, 409) });
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText("Слишком много незавершённых загрузок")).toBeInTheDocument();
    expect(FakeXhr.all).toHaveLength(0);
  });

  it("422 по названию — ошибка у поля", async () => {
    api({
      "POST /api/uploads": () =>
        json({ detail: "Ошибка проверки", errors: [{ loc: ["body", "title"], msg: "Название без видимых символов" }] }, 422),
    });
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText("Название без видимых символов")).toBeInTheDocument();
  });

  it("часть отклонена (403) — понятное сообщение, незавершённая загрузка удалена", async () => {
    FakeXhr.mode = "403";
    const fetchMock = api();
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText(/Ссылки на загрузку истекли/)).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(1));
    expect(calls(fetchMock, "POST /api/uploads/v9/complete")).toHaveLength(0);
    // Форма снова доступна.
    await new Promise((r) => setTimeout(r, 20)); // запоздалые события частей
    expect(screen.getByRole("button", { name: "Загрузить" })).toBeEnabled();
  });

  it("«Отменить» прерывает отправку частей и удаляет загрузку", async () => {
    FakeXhr.mode = "hold";
    const fetchMock = api();
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    await waitFor(() => expect(FakeXhr.all).toHaveLength(3));
    expect(screen.getByLabelText("Файл")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    expect(await screen.findByText("Загрузка отменена.")).toBeInTheDocument();
    expect(FakeXhr.all.every((x) => x.aborted)).toBe(true);
    await waitFor(() => expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(1));
    expect(calls(fetchMock, "POST /api/uploads/v9/complete")).toHaveLength(0);
  });

  it("уход со страницы во время загрузки — отмена и удаление загрузки", async () => {
    FakeXhr.mode = "hold";
    const fetchMock = api();
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    await waitFor(() => expect(FakeXhr.all).toHaveLength(3));
    fireEvent.click(screen.getByRole("button", { name: "уйти" }));
    expect(await screen.findByText("другая страница")).toBeInTheDocument();
    expect(FakeXhr.all.every((x) => x.aborted)).toBe(true);
    await waitFor(() => expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(1));
  });

  it("уход, пока старт ещё не ответил — загрузка удаляется после ответа, части не отправляются", async () => {
    let release: () => void = () => undefined;
    const fetchMock = api({
      "POST /api/uploads": async () => {
        await new Promise<void>((r) => (release = r));
        return json(STARTED, 201);
      },
    });
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    await waitFor(() => expect(calls(fetchMock, "POST /api/uploads")).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "уйти" }));
    release();
    await waitFor(() => expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(1));
    expect(FakeXhr.all).toHaveLength(0);
  });

  it("complete → 400: сообщение сервера, загрузка удалена", async () => {
    const fetchMock = api({ "POST /api/uploads/v9/complete": () => json({ detail: "Не все части загружены" }, 400) });
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText("Не все части загружены")).toBeInTheDocument();
    await waitFor(() => expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(1));
  });

  it("complete: ответ потерян (409 при повторе), видео в обработке — «Видео загружено», без удаления", async () => {
    let n = 0;
    const fetchMock = api({
      "POST /api/uploads/v9/complete": () => (n++ === 0 ? json({ detail: "x" }, 504) : json({ detail: "Уже завершена" }, 409)),
      "GET /api/videos/v9": () => ({ id: "v9", title: "t", state: { status: "processing" } }),
    });
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByRole("heading", { name: "Видео загружено" }, { timeout: 4000 })).toBeInTheDocument();
    expect(calls(fetchMock, "POST /api/uploads/v9/complete")).toHaveLength(2);
    expect(calls(fetchMock, "DELETE /api/uploads/v9")).toHaveLength(0);
  });

  it("смена файла заменяет автоназвание, но не название, введённое вручную", () => {
    api();
    render();
    pick(videoFile("первый.mp4"));
    expect(screen.getByLabelText("Название")).toHaveValue("первый");
    pick(videoFile("второй.mp4"));
    expect(screen.getByLabelText("Название")).toHaveValue("второй");
    fireEvent.change(screen.getByLabelText("Название"), { target: { value: "Моё" } });
    pick(videoFile("третий.mp4"));
    expect(screen.getByLabelText("Название")).toHaveValue("Моё");
  });

  it("422 по размеру файла — ошибка у поля «Файл»", async () => {
    api({
      "POST /api/uploads": () =>
        json({ detail: "Ошибка проверки", errors: [{ loc: ["body", "size_bytes"], msg: "Файл больше лимита" }] }, 422),
    });
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText("Файл больше лимита")).toBeInTheDocument();
    expect(screen.getByLabelText("Файл")).toHaveAttribute("aria-invalid", "true");
  });

  it("во время загрузки закрытие вкладки предупреждает (beforeunload)", async () => {
    FakeXhr.mode = "hold";
    api();
    render();
    pick(videoFile());
    fireEvent.click(screen.getByRole("button", { name: "Загрузить" }));
    await waitFor(() => expect(FakeXhr.all).toHaveLength(3));
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    await screen.findByText("Загрузка отменена.");
    const after = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(false);
  });
});
