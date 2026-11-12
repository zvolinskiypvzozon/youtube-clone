import { describe, expect, it } from "vitest";
import { createWatchTracker, viewThreshold } from "./watchTracker";

function play(tracker: ReturnType<typeof createWatchTracker>, from: number, to: number, threshold: number) {
  let fired = 0;
  for (let t = from; t <= to + 1e-9; t += 0.25) if (tracker.update(t, threshold)) fired++;
  return fired;
}

describe("viewThreshold", () => {
  it("is min(30 s, half of duration)", () => {
    expect(viewThreshold(10)).toBe(5);
    expect(viewThreshold(60)).toBe(30);
    expect(viewThreshold(3600)).toBe(30);
  });

  it("falls back to 30 s when duration is unknown", () => {
    expect(viewThreshold(null)).toBe(30);
    expect(viewThreshold(Number.NaN)).toBe(30);
    expect(viewThreshold(Number.POSITIVE_INFINITY)).toBe(30);
    expect(viewThreshold(0)).toBe(30);
  });
});

describe("createWatchTracker", () => {
  it("fires exactly once after the threshold of real playback", () => {
    const tracker = createWatchTracker();
    expect(play(tracker, 0, 4.75, 5)).toBe(0);
    expect(play(tracker, 5, 20, 5)).toBe(1);
    expect(play(tracker, 20.25, 40, 5)).toBe(0);
  });

  it("does not count a seek forward", () => {
    const tracker = createWatchTracker();
    tracker.update(0, 5);
    expect(tracker.update(100, 5)).toBe(false);
    expect(tracker.played).toBe(0);
  });

  it("does not count jumps after seek() and resumes counting from the new position", () => {
    const tracker = createWatchTracker();
    play(tracker, 0, 2, 5);
    tracker.seek();
    tracker.update(50, 5);
    expect(tracker.played).toBeCloseTo(2);
    expect(play(tracker, 50.25, 53, 5)).toBe(1);
  });

  it("seek() discards a small jump that MAX_STEP alone would count", () => {
    const tracker = createWatchTracker();
    play(tracker, 0, 2, 30);
    tracker.seek();
    tracker.update(3, 30); // перемотка на 1 с вперёд — меньше MAX_STEP
    expect(tracker.played).toBeCloseTo(2);
  });

  it("does not count a rewind", () => {
    const tracker = createWatchTracker();
    play(tracker, 0, 3, 5);
    tracker.update(0, 5);
    expect(tracker.played).toBeCloseTo(3);
  });
});
