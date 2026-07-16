import { afterEach, describe, expect, it, vi } from "vitest";
import { formatTime } from "./utils";

afterEach(() => {
  vi.useRealTimers();
});

describe("formatTime", () => {
  it("shows the local time for an earlier timestamp on the same day", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 6, 16, 17, 45));

    expect(formatTime(new Date(2026, 6, 16, 0, 15).toISOString())).toBe("00:15");
  });

  it("shows yesterday immediately after crossing local midnight", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 6, 16, 0, 30));

    expect(formatTime(new Date(2026, 6, 15, 23, 30).toISOString())).toBe("昨天");
  });

  it("uses calendar-day labels for recent older timestamps", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 6, 16, 12, 0));

    expect(formatTime(new Date(2026, 6, 14, 18, 0).toISOString())).toBe("2天前");
  });
});
