import { afterEach, describe, expect, it, vi } from "vitest";
import {
  PART_ATTEMPTS,
  PART_TIMEOUT_MS,
  UploadPartError,
  uploadErrorMessage,
  uploadParts,
  xhrPutPart,
  type PutPart,
} from "./uploadParts";

const noSleep = () => Promise.resolve();

function partsFor(sizes: number[]) {
  return sizes.map((size, i) => ({ number: i + 1, size, url: `/yt-raw/raw/v1/source?partNumber=${i + 1}&X-Amz-Signature=s` }));
}

function file(bytes: number): Blob {
  return new Blob([new Uint8Array(Array.from({ length: bytes }, (_, i) => i % 251))]);
}

/** Байты среза (в jsdom у Blob нет arrayBuffer — читаем через FileReader). */
function bytesOf(blob: Blob): Promise<number[]> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(Array.from(new Uint8Array(reader.result as ArrayBuffer)));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

describe("uploadParts", () => {
  it("режет файл по размерам частей (срезы Blob), ETag — по номеру части", async () => {
    const src = file(25);
    const bodies = new Map<string, Blob>();
    const put: PutPart = async (url, body) => {
      bodies.set(url, body);
      return `"etag-${new URL(url, "http://x").searchParams.get("partNumber")}"`;
    };
    const parts = partsFor([10, 10, 5]);
    const result = await uploadParts(src, parts, { signal: new AbortController().signal, putPart: put, sleep: noSleep });
    expect(result).toEqual([
      { number: 1, etag: '"etag-1"' },
      { number: 2, etag: '"etag-2"' },
      { number: 3, etag: '"etag-3"' },
    ]);
    const all = await bytesOf(src);
    expect(await bytesOf(bodies.get(parts[0].url)!)).toEqual(all.slice(0, 10));
    expect(await bytesOf(bodies.get(parts[1].url)!)).toEqual(all.slice(10, 20));
    expect(await bytesOf(bodies.get(parts[2].url)!)).toEqual(all.slice(20, 25));
  });

  it("не больше 3 частей одновременно; результат по возрастанию номера при любом порядке ответов", async () => {
    // Ответы — по команде теста, в обратном порядке: результат всё равно по номеру части.
    const pending = new Map<number, (etag: string) => void>();
    let maxInFlight = 0;
    const put: PutPart = (url) =>
      new Promise((resolve) => {
        const n = Number(new URL(url, "http://x").searchParams.get("partNumber"));
        pending.set(n, resolve);
        maxInFlight = Math.max(maxInFlight, pending.size);
      });
    const promise = uploadParts(file(7), partsFor([1, 1, 1, 1, 1, 1, 1]), {
      signal: new AbortController().signal,
      putPart: put,
    });
    const flush = () => new Promise((r) => setTimeout(r, 0));
    while (true) {
      await flush();
      if (pending.size === 0) break;
      const last = Math.max(...pending.keys());
      const resolve = pending.get(last)!;
      pending.delete(last);
      resolve(`e${last}`);
    }
    const result = await promise;
    expect(maxInFlight).toBe(3);
    expect(result.map((p) => p.number)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(result.map((p) => p.etag)).toEqual(["e1", "e2", "e3", "e4", "e5", "e6", "e7"]);
  });

  it("прогресс: сумма по частям, в конце — весь размер; сбой части обнуляет её вклад", async () => {
    const seen: number[] = [];
    let first = true;
    const put: PutPart = async (_url, body, onProgress) => {
      onProgress(body.size / 2);
      if (first) {
        first = false;
        throw new UploadPartError(503, "x");
      }
      onProgress(body.size);
      return "e";
    };
    await uploadParts(file(10), partsFor([10]), {
      signal: new AbortController().signal,
      putPart: put,
      sleep: noSleep,
      onProgress: (loaded, total) => {
        expect(total).toBe(10);
        seen.push(loaded);
      },
    });
    expect(seen).toEqual([5, 0, 5, 10, 10]);
  });

  it.each([0, 500, 503])(`сбой %i повторяется (до ${PART_ATTEMPTS} попыток)`, async (status) => {
    let calls = 0;
    const put: PutPart = async () => {
      if (++calls < PART_ATTEMPTS) throw new UploadPartError(status, "x");
      return "e";
    };
    await expect(uploadParts(file(1), partsFor([1]), { signal: new AbortController().signal, putPart: put, sleep: noSleep })).resolves.toEqual([
      { number: 1, etag: "e" },
    ]);
    expect(calls).toBe(PART_ATTEMPTS);
  });

  it(`после ${PART_ATTEMPTS} неудач — ошибка`, async () => {
    const put = vi.fn<PutPart>(async () => {
      throw new UploadPartError(500, "x");
    });
    await expect(uploadParts(file(1), partsFor([1]), { signal: new AbortController().signal, putPart: put, sleep: noSleep })).rejects.toThrow(
      UploadPartError,
    );
    expect(put).toHaveBeenCalledTimes(PART_ATTEMPTS);
  });

  it("403 (истёкшая подпись) и другие 4xx не повторяются", async () => {
    const put = vi.fn<PutPart>(async () => {
      throw new UploadPartError(403, "x");
    });
    await expect(uploadParts(file(1), partsFor([1]), { signal: new AbortController().signal, putPart: put, sleep: noSleep })).rejects.toMatchObject({
      status: 403,
    });
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("окончательная ошибка одной части прерывает остальные и не запускает новые", async () => {
    const signals: AbortSignal[] = [];
    const started: number[] = [];
    const put: PutPart = (url, _b, _p, signal) => {
      const n = Number(new URL(url, "http://x").searchParams.get("partNumber"));
      started.push(n);
      signals.push(signal);
      if (n === 1) return Promise.reject(new UploadPartError(403, "x"));
      return new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("a", "AbortError"))));
    };
    await expect(
      uploadParts(file(6), partsFor([1, 1, 1, 1, 1, 1]), { signal: new AbortController().signal, putPart: put, sleep: noSleep }),
    ).rejects.toMatchObject({ status: 403 });
    expect(signals.every((s) => s.aborted)).toBe(true);
    expect(started.sort()).toEqual([1, 2, 3]);
  });

  it("отмена пользователем — AbortError, без повторов", async () => {
    const controller = new AbortController();
    const put = vi.fn<PutPart>(
      (_u, _b, _p, signal) =>
        new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("a", "AbortError")))),
    );
    const promise = uploadParts(file(2), partsFor([1, 1]), { signal: controller.signal, putPart: put, sleep: noSleep });
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(put).toHaveBeenCalledTimes(2);
  });

  it.each([
    "/api/videos/v1",
    "//evil.example/yt-raw/x",
    "https://evil.example/yt-raw/x",
    "/yt-raw\\..\\api",
    "/yt-media/x",
    "/yt-raw/../api/me/videos",
    "/yt-raw/%2e%2e/api/x",
    "/yt-raw/.%2E/api/x",
    "/yt-raw/%2E%2e/%2e%2e/api",
  ])("ссылка %j вне бакета загрузки — отказ до отправки", async (url) => {
    const put = vi.fn<PutPart>();
    await expect(
      uploadParts(file(1), [{ number: 1, size: 1, url }], { signal: new AbortController().signal, putPart: put }),
    ).rejects.toThrow("Недопустимая ссылка");
    expect(put).not.toHaveBeenCalled();
  });

  it("отмена во время паузы перед повтором — сразу AbortError, без ожидания конца паузы", async () => {
    const controller = new AbortController();
    const put = vi.fn<PutPart>(async () => {
      throw new UploadPartError(503, "x");
    });
    const promise = uploadParts(file(1), partsFor([1]), { signal: controller.signal, putPart: put });
    await vi.waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const started = Date.now();
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
    expect(Date.now() - started).toBeLessThan(500);
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("сумма частей не равна размеру файла — отказ до отправки", async () => {
    const put = vi.fn<PutPart>();
    await expect(
      uploadParts(file(5), partsFor([2, 2]), { signal: new AbortController().signal, putPart: put }),
    ).rejects.toThrow("не совпадает");
    expect(put).not.toHaveBeenCalled();
  });

  it("сообщения: сеть, 403, прочее", () => {
    expect(uploadErrorMessage(new UploadPartError(0, ""))).toMatch(/Нет связи/);
    expect(uploadErrorMessage(new UploadPartError(403, ""))).toMatch(/истекли/);
    expect(uploadErrorMessage(new UploadPartError(500, ""))).toMatch(/500/);
  });
});

/** Подмена XMLHttpRequest: тест управляет ответом. */
class FakeXhr {
  static last: FakeXhr;
  method = "";
  url = "";
  body: unknown;
  status = 0;
  headers: Record<string, string> = {};
  upload: { onprogress: ((e: { loaded: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  timeout = 0;
  aborted = false;
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  send(body: unknown) {
    this.body = body;
  }
  setRequestHeader() {
    throw new Error("заголовки не ставятся");
  }
  getResponseHeader(name: string) {
    return this.headers[name] ?? null;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  respond(status: number, headers: Record<string, string> = {}) {
    this.status = status;
    this.headers = headers;
    this.onload?.();
  }
}

describe("xhrPutPart", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("PUT среза без заголовков, прогресс, ETag из ответа", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const body = new Blob(["abc"]);
    const progress = vi.fn();
    const promise = xhrPutPart("/yt-raw/x", body, progress, new AbortController().signal);
    const xhr = FakeXhr.last;
    expect([xhr.method, xhr.url, xhr.body]).toEqual(["PUT", "/yt-raw/x", body]);
    xhr.upload.onprogress?.({ loaded: 2 });
    expect(progress).toHaveBeenCalledWith(2);
    xhr.respond(200, { ETag: '"abc"' });
    await expect(promise).resolves.toBe('"abc"');
  });

  it("403 — UploadPartError(403); 200 без ETag — ошибка", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const p1 = xhrPutPart("/yt-raw/x", new Blob(["a"]), () => undefined, new AbortController().signal);
    FakeXhr.last.respond(403);
    await expect(p1).rejects.toMatchObject({ status: 403 });
    const p2 = xhrPutPart("/yt-raw/x", new Blob(["a"]), () => undefined, new AbortController().signal);
    FakeXhr.last.respond(200);
    await expect(p2).rejects.toBeInstanceOf(UploadPartError);
  });

  it("таймаут части — сетевая ошибка (повторяемая), таймаут задан", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const promise = xhrPutPart("/yt-raw/x", new Blob(["a"]), () => undefined, new AbortController().signal);
    expect(FakeXhr.last.timeout).toBe(PART_TIMEOUT_MS);
    FakeXhr.last.ontimeout?.();
    await expect(promise).rejects.toMatchObject({ status: 0 });
  });

  it("сигнал отмены прерывает XHR", async () => {
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const controller = new AbortController();
    const promise = xhrPutPart("/yt-raw/x", new Blob(["a"]), () => undefined, controller.signal);
    controller.abort();
    expect(FakeXhr.last.aborted).toBe(true);
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
  });
});
