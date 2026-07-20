import { afterEach, describe, expect, it, vi } from "vitest";
import { escapeMarkdownImageAlt, formatTime } from "./utils";

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

describe("escapeMarkdownImageAlt", () => {
  it("escapes brackets and backslashes in pasted image filenames", () => {
    expect(escapeMarkdownImageAlt("IMG_30[1].PNG")).toBe("IMG_30&#91;1&#93;.PNG");
    expect(escapeMarkdownImageAlt("截图\\备份[2].png")).toBe("截图\\备份&#91;2&#93;.png");
  });
});
