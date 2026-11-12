// Проверка типов: этот файл компилирует `tsc` (npm run build, CI). Каждая строка, помеченная
// директивой ts-expect-error, обязана давать ошибку компиляции — иначе tsc падает. Так
// проверено, что неверный метод, путь, параметры и тело не компилируются.
import { describe, expect, it } from "vitest";
import { api, buildPath, type RouteKey } from "./typed";

const never = false;

describe("api: сверка со схемой OpenAPI на этапе компиляции", () => {
  it("неверные вызовы не компилируются", () => {
    if (never) {
      // Верно: бан — PUT.
      void api("put", "/api/admin/users/{user_id}/ban", { params: { user_id: "u" }, body: { reason: "r" } });
      // @ts-expect-error — бан не POST (ручная проверка дня 8: 405)
      void api("post", "/api/admin/users/{user_id}/ban", { params: { user_id: "u" }, body: { reason: "r" } });
      // @ts-expect-error — такого пути нет
      void api("get", "/api/video/{video_id}", { params: { video_id: "v" } });
      // @ts-expect-error — не тот параметр пути
      void api("get", "/api/videos/{video_id}", { params: { id: "v" } });
      // @ts-expect-error — параметры пути обязательны
      void api("get", "/api/videos/{video_id}");
      // @ts-expect-error — тело не по схеме
      void api("put", "/api/videos/{video_id}/reaction", { params: { video_id: "v" }, body: { kind: "love" } });
      // @ts-expect-error — тело обязательно
      void api("post", "/api/videos/{video_id}/comments", { params: { video_id: "v" } });
      // Необязательное тело (requestBody?: в схеме): можно передать и можно не передавать.
      void api("post", "/api/admin/reports/{report_id}/dismiss", { params: { report_id: "r" }, body: { reason: "x" } });
      void api("post", "/api/admin/reports/{report_id}/dismiss", { params: { report_id: "r" } });
      // @ts-expect-error — необязательное тело тоже сверяется со схемой
      void api("post", "/api/admin/reports/{report_id}/dismiss", { params: { report_id: "r" }, body: { why: "x" } });
      // @ts-expect-error — у операции без тела тело передать нельзя
      void api("get", "/api/videos/{video_id}", { params: { video_id: "v" }, body: { x: 1 } });
    }
    expect(true).toBe(true);
  });

  it("ключи моков в тестах — только метод и путь из схемы", () => {
    const ok: RouteKey[] = ["PUT /api/admin/users/u8/ban", "GET /api/videos/v1", "DELETE /api/comments/k1"];
    // @ts-expect-error — мок на неверный метод не компилируется
    const wrong: RouteKey = "POST /api/admin/users/u8/ban";
    expect(ok.length + (wrong ? 1 : 0)).toBe(4);
  });

  it("buildPath подставляет и кодирует параметры", () => {
    expect(buildPath("/api/channels/{handle}/videos", { handle: "a/b?c" })).toBe("/api/channels/a%2Fb%3Fc/videos");
    expect(() => buildPath("/api/videos/{video_id}", {})).toThrow("video_id");
  });
});
