import { describe, expect, it } from "vitest";
import { formatDuration, formatRelative, formatViews } from "./format";

describe("формат", () => {
  it("просмотры: склонение и сокращение", () => {
    expect(formatViews(0)).toBe("0 просмотров");
    expect(formatViews(1)).toBe("1 просмотр");
    expect(formatViews(3)).toBe("3 просмотра");
    expect(formatViews(11)).toBe("11 просмотров");
    expect(formatViews(21)).toBe("21 просмотр");
    // Intl ставит неразрывный пробел перед «тыс.».
    expect(formatViews(1200)).toMatch(/^1,2\sтыс\. просмотров$/);
  });

  it("длительность", () => {
    expect(formatDuration(5)).toBe("0:05");
    expect(formatDuration(754)).toBe("12:34");
    expect(formatDuration(3723)).toBe("1:02:03");
  });

  it("относительная дата", () => {
    const now = new Date("2026-11-11T12:00:00Z");
    expect(formatRelative("2026-11-11T11:59:50Z", now)).toBe("только что");
    expect(formatRelative("2026-11-11T11:55:00Z", now)).toBe("5 минут назад");
    expect(formatRelative("2026-11-10T12:00:00Z", now)).toBe("вчера");
    expect(formatRelative("2026-10-21T12:00:00Z", now)).toBe("3 недели назад");
  });
});
