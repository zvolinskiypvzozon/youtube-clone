import { describe, expect, it } from "vitest";
import { mockFetchRaw as mockApi, json } from "../test/utils";
import { ApiError, CSRF_HEADER, NetworkError, parseRetryAfter, request } from "./client";

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const error = await promise.then(
    () => null,
    (e: unknown) => e,
  );
  if (!(error instanceof ApiError)) throw new Error(`ожидалась ApiError, получено ${String(error)}`);
  return error;
}

describe("request", () => {
  it("добавляет CSRF-заголовок из cookie на изменяющих запросах", async () => {
    document.cookie = "csrf=token-123; path=/";
    const fetchMock = mockApi({ "POST /api/x": () => ({ ok: true }), "GET /api/x": () => ({ ok: true }) });
    await request("/api/x", { method: "POST", body: { a: 1 } });
    await request("/api/x");
    const [post, get] = fetchMock.mock.calls.map(([, init]) => (init?.headers ?? {}) as Record<string, string>);
    expect(post[CSRF_HEADER]).toBe("token-123");
    expect(post["Content-Type"]).toBe("application/json");
    expect(get[CSRF_HEADER]).toBeUndefined();
    expect(fetchMock.mock.calls[0][1]?.credentials).toBe("same-origin");
  });

  it("без cookie csrf заголовок не отправляется", async () => {
    const fetchMock = mockApi({ "DELETE /api/x": () => new Response(null, { status: 204 }) });
    await expect(request("/api/x", { method: "DELETE" })).resolves.toBeUndefined();
    expect((fetchMock.mock.calls[0][1]?.headers as Record<string, string>)[CSRF_HEADER]).toBeUndefined();
  });

  it("422 → ApiError с полями (последний элемент loc)", async () => {
    mockApi({
      "POST /api/x": () =>
        json({ detail: "Проверьте правильность полей: email", errors: [{ loc: ["body", "email"], msg: "bad" }] }, 422),
    });
    const error = await failure(request("/api/x", { method: "POST", body: {} }));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(422);
    expect(error.fields).toEqual({ email: "bad" });
    expect(error.message).toBe("Проверьте правильность полей: email");
  });

  it("429 и 503 — русское сообщение и Retry-After", async () => {
    mockApi({
      "GET /api/a": () => new Response("<html>429</html>", { status: 429 }),
      "GET /api/b": () => json({ detail: "busy" }, 503, { "Retry-After": "2" }),
    });
    const a = await failure(request("/api/a"));
    const b = await failure(request("/api/b"));
    expect(a.message).toMatch(/Слишком много запросов/);
    expect(a.retryAfter).toBeNull();
    expect(b.message).toMatch(/Сервер занят/);
    expect(b.retryAfter).toBe(2);
  });

  it("сбой сети → NetworkError", async () => {
    mockApi({ "GET /api/x": () => Promise.reject(new TypeError("Failed to fetch")) });
    await expect(request("/api/x")).rejects.toBeInstanceOf(NetworkError);
  });

  it("параметры запроса: пустые значения не передаются", async () => {
    const fetchMock = mockApi({ "GET /api/videos": () => ({ items: [], next_cursor: null }) });
    await request("/api/videos", { query: { q: "", cursor: null, limit: 24 } });
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/videos?limit=24");
  });
});

describe("parseRetryAfter", () => {
  it("секунды, дата, мусор", () => {
    expect(parseRetryAfter("5")).toBe(5);
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
    expect(parseRetryAfter(new Date(Date.now() + 10_000).toUTCString())).toBeGreaterThanOrEqual(8);
  });
});
