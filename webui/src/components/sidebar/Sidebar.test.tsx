// @vitest-environment jsdom

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";

afterEach(cleanup);

describe("Sidebar sessions", () => {
  it("selects a session and keeps edit actions independent", () => {
    const onSelect = vi.fn();
    const onRename = vi.fn();
    const view = render(
      <Sidebar
        sessions={[{
          sessionId: "session-a",
          title: "会话 A",
          messageCount: 3,
          updatedAt: "2026-07-13T10:00:00Z",
        }]}
        activeSessionId={null}
        loading={false}
        onNewChat={vi.fn()}
        onSelect={onSelect}
        onDelete={vi.fn()}
        onRename={onRename}
        onOpenSettings={vi.fn()}
      />
    );

    const sessionTitle = view.getByText("会话 A");
    const sessionItem = sessionTitle.closest('[role="button"]');
    expect(sessionItem).not.toBeNull();

    fireEvent.click(sessionItem!);
    expect(onSelect).toHaveBeenCalledWith("session-a");

    fireEvent.mouseEnter(sessionItem!);
    const rename = view.getByTitle("重命名");
    expect(rename.parentElement?.className).toContain("top-1.5");
    fireEvent.click(rename);

    expect(onRename).toHaveBeenCalledWith("session-a");
    expect(onSelect).toHaveBeenCalledTimes(1);
  });
});
