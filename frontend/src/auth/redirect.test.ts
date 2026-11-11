import { describe, expect, it } from "vitest";
import { safeReturnPath } from "./redirect";

describe("safeReturnPath", () => {
  it.each([
    ["/upload", "/upload"],
    ["/?q=кот", "/?q=кот"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
    ["https://evil.example", "/"],
    [["javascript", "alert(1)"].join(":"), "/"], // склейка: правило no-script-url
    ["/login", "/"],
    [undefined, "/"],
    [42, "/"],
    ["/\t/evil.example", "/"],
    ["/\n/evil.example", "/"],
    ["/a\\b", "/"],
    ["/%5Cevil.example", "/"],
    ["/%09/evil.example", "/"],
    ["/my/videos?x=1#top", "/my/videos?x=1#top"],
  ])("%s → %s", (input, expected) => {
    expect(safeReturnPath(input)).toBe(expected);
  });
});
