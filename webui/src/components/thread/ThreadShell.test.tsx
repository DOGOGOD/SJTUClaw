// @vitest-environment jsdom

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionModeBadges, ThreadShell } from "./ThreadShell";

const animationFrames: FrameRequestCallback[] = [];
const scrollTo = vi.fn();
let resizeObserverCallback: ResizeObserverCallback | null = null;

class ResizeObserverMock {
  constructor(callback: ResizeObserverCallback) {
    resizeObserverCallback = callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  animationFrames.length = 0;
  resizeObserverCallback = null;
  scrollTo.mockReset();
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
    animationFrames.push(callback);
    return animationFrames.length;
  }));
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: scrollTo,
  });
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function shellProps(sessionId: string, messages: Array<{ role: "user" | "assistant"; content: string }>) {
  return {
    sessionId,
    title: "测试会话",
    messages,
    loading: false,
    sending: false,
    onSend: vi.fn().mockResolvedValue(undefined),
    onNewChat: vi.fn(),
    theme: "light" as const,
    onToggleTheme: vi.fn(),
  };
}

function flushAnimationFrames() {
  act(() => {
    animationFrames.splice(0).forEach((callback) => callback(performance.now()));
  });
}

describe("SessionModeBadges", () => {
  it("shows the Sandbox indicator only for sandboxed sessions", () => {
    const view = render(
      <SessionModeBadges
        autoMode={false}
        sandboxMode
        unlimitedMode={false}
        piMode={false}
      />
    );
    expect(view.getByTestId("sandbox-mode-badge").textContent).toContain(
      "Sandbox"
    );

    view.rerender(
      <SessionModeBadges
        autoMode={false}
        sandboxMode={false}
        unlimitedMode={false}
        piMode={false}
      />
    );
    expect(view.queryByTestId("sandbox-mode-badge")).toBeNull();
  });

  it("shows the Pi indicator only for sessions with Pi enabled", () => {
    const view = render(
      <SessionModeBadges autoMode={false} unlimitedMode={false} piMode />
    );
    expect(view.getByTestId("pi-mode-badge").textContent).toContain("Pi");

    view.rerender(
      <SessionModeBadges autoMode={false} unlimitedMode={false} piMode={false} />
    );
    expect(view.queryByTestId("pi-mode-badge")).toBeNull();
  });

  it("shows the Rollback indicator only when rollback is enabled", () => {
    const view = render(
      <SessionModeBadges
        autoMode={false}
        unlimitedMode={false}
        rollbackEnabled
        piMode={false}
      />
    );
    expect(view.getByTestId("rollback-mode-badge").textContent).toContain(
      "Rollback"
    );

    view.rerender(
      <SessionModeBadges
        autoMode={false}
        unlimitedMode={false}
        rollbackEnabled={false}
        piMode={false}
      />
    );
    expect(view.queryByTestId("rollback-mode-badge")).toBeNull();
  });

  it("shows the Claude Code indicator for Claude sessions", () => {
    const view = render(
      <SessionModeBadges
        autoMode={false}
        unlimitedMode={false}
        piMode={false}
        agentBackend="claude"
      />
    );
    expect(view.getByTestId("claude-mode-badge").textContent).toContain(
      "Claude Code"
    );
  });
});

describe("ThreadShell session scrolling", () => {
  it("opens each session at its latest message", () => {
    const view = render(
      <ThreadShell
        {...shellProps("session-a", [
          { role: "user", content: "第一条消息" },
          { role: "assistant", content: "最新消息 A" },
        ])}
      />,
    );
    const viewport = view.getByTestId("thread-scroll-viewport");
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 1200 });

    flushAnimationFrames();
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 1200, behavior: "instant" });

    scrollTo.mockClear();
    view.rerender(
      <ThreadShell
        {...shellProps("session-b", [
          { role: "user", content: "另一会话的首条消息" },
          { role: "assistant", content: "最新消息 B" },
        ])}
      />,
    );
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 1800 });

    flushAnimationFrames();
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 1800, behavior: "instant" });
  });

  it("keeps the latest message visible when asynchronous content expands", () => {
    const view = render(
      <ThreadShell
        {...shellProps("session-a", [
          { role: "user", content: "第一条消息" },
          { role: "assistant", content: "包含延迟渲染内容的最新消息" },
        ])}
      />,
    );
    const viewport = view.getByTestId("thread-scroll-viewport");
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 1200 });
    flushAnimationFrames();
    scrollTo.mockClear();

    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 2000 });
    act(() => {
      resizeObserverCallback?.([], {} as ResizeObserver);
    });
    flushAnimationFrames();

    expect(scrollTo).toHaveBeenLastCalledWith({ top: 2000, behavior: "instant" });
  });

  it("does not pull the user back down after they scroll up", () => {
    const view = render(
      <ThreadShell
        {...shellProps("session-a", [
          { role: "user", content: "第一条消息" },
          { role: "assistant", content: "最新消息" },
        ])}
      />,
    );
    const viewport = view.getByTestId("thread-scroll-viewport");
    Object.defineProperties(viewport, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    });
    flushAnimationFrames();
    scrollTo.mockClear();

    fireEvent.scroll(viewport);
    view.rerender(
      <ThreadShell
        {...shellProps("session-a", [
          { role: "user", content: "第一条消息" },
          { role: "assistant", content: "最新消息" },
          { role: "assistant", content: "后台刷新出的新消息" },
        ])}
      />,
    );
    flushAnimationFrames();

    expect(scrollTo).not.toHaveBeenCalled();
  });
});
