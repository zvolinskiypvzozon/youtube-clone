import { describe, expect, it } from "vitest";
import { initial } from "./VideoCard";

describe("initial", () => {
  it("целый символ, а не половина суррогатной пары", () => {
    expect(initial("😀 Кот")).toBe("😀");
    expect(initial("кот")).toBe("К");
    expect(initial("  ")).toBe("?");
  });
});
