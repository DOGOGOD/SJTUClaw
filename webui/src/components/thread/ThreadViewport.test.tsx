// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ThreadViewport } from "./ThreadViewport";

beforeEach(() => window.localStorage.clear());
afterEach(cleanup);

describe("ThreadViewport user messages", () => {
  it("centers user text and supports preset or local avatars from the context menu", async () => {
    const view = render(
      <ThreadViewport
        messages={[{ role: "user", content: "你好呀" }]}
        loading={false}
        sessionId="session-a"
      />
    );

    const bubble = view.getByText("你好呀").closest(".flex.min-h-10");
    expect(bubble?.className).toContain("items-center");
    expect(view.getByText("你好呀").parentElement?.className).toContain("user-message-content");

    const avatar = view.getByRole("button", { name: /用户头像/ });
    fireEvent.contextMenu(avatar);
    const menu = view.getByRole("menu", { name: "选择用户头像" });
    expect(menu.className).toContain("fixed");
    expect(menu.parentElement).toBe(document.body);
    expect(view.getByRole("button", { name: "导入本地图片" })).toBeTruthy();

    fireEvent.change(view.getByLabelText("导入本地头像图片"), {
      target: { files: [new File(["not an image"], "avatar.svg", { type: "image/svg+xml" })] },
    });
    await waitFor(() => {
      expect(view.getByRole("alert").textContent).toContain("请选择 PNG");
    });

    fireEvent.click(view.getByTitle("猫咪"));
    expect(window.localStorage.getItem("sjtuclaw.user-avatar")).toBe("cat");
    expect(view.getByRole("button", { name: /猫咪/ })).toBeTruthy();
  });
});
