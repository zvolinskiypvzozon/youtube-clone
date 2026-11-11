import { describe, expect, it } from "vitest";
import { ApiError, NetworkError } from "./client";
import { createQueryClient } from "./queryClient";
import { MAX_RETRIES, retryDelay, shouldRetry, shouldRetryMutation } from "./retry";

describe("политика повторов", () => {
  it.each([400, 401, 403, 404, 409, 422])("%i — без повторов", (status) => {
    expect(shouldRetry(0, new ApiError(status, "x"))).toBe(false);
  });

  it.each([429, 503])("%i — повтор до предела", (status) => {
    const error = new ApiError(status, "x");
    expect(shouldRetry(0, error)).toBe(true);
    expect(shouldRetry(MAX_RETRIES - 1, error)).toBe(true);
    expect(shouldRetry(MAX_RETRIES, error)).toBe(false);
  });

  it("500 и прочие ошибки — без повторов; сеть — один повтор", () => {
    expect(shouldRetry(0, new ApiError(500, "x"))).toBe(false);
    expect(shouldRetry(0, new Error("x"))).toBe(false);
    expect(shouldRetry(0, new NetworkError())).toBe(true);
    expect(shouldRetry(1, new NetworkError())).toBe(false);
  });

  it("задержка — из Retry-After (с потолком), иначе экспонента", () => {
    expect(retryDelay(0, new ApiError(503, "x", {}, 2))).toBe(2000);
    expect(retryDelay(0, new ApiError(503, "x", {}, 0))).toBe(1000);
    expect(retryDelay(0, new ApiError(429, "x", {}, 3600))).toBe(30_000);
    expect(retryDelay(0, new ApiError(429, "x"))).toBe(1000);
    expect(retryDelay(2, new ApiError(429, "x"))).toBe(4000);
  });

  it("мутации: повтор только на 429 (503 мог прийти после записи)", () => {
    expect(shouldRetryMutation(0, new ApiError(429, "x"))).toBe(true);
    expect(shouldRetryMutation(MAX_RETRIES, new ApiError(429, "x"))).toBe(false);
    expect(shouldRetryMutation(0, new ApiError(503, "x"))).toBe(false);
    expect(shouldRetryMutation(0, new NetworkError())).toBe(false);
  });

  it("политика подключена к QueryClient", () => {
    const defaults = createQueryClient().getDefaultOptions();
    expect(defaults.queries?.retry).toBe(shouldRetry);
    expect(defaults.queries?.retryDelay).toBe(retryDelay);
    expect(defaults.mutations?.retry).toBe(shouldRetryMutation);
  });
});

