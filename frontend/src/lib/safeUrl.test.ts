import { describe, expect, it } from "vitest";
import { safeUrl } from "./safeUrl";

describe("safeUrl", () => {
  it.each([
    ["/yt-media/videos/v1/thumb.jpg?X-Amz-Signature=abc", "/yt-media/videos/v1/thumb.jpg?X-Amz-Signature=abc"],
    ["//evil.example/x.jpg", undefined],
    ["/\\evil.example", undefined],
    ["/\t/evil.example", undefined],
    ["https://evil.example/x.jpg", undefined],
    [["javascript", "alert(1)"].join(":"), undefined], // склейка: правило no-script-url
    ["data:image/svg+xml,<svg/>", undefined],
    [null, undefined],
    ["", undefined],
  ])("%s → %s", (input, expected) => {
    expect(safeUrl(input)).toBe(expected);
  });
});
