// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

  it("renders network and workspace-local images inside messages", () => {
    const view = render(
      <ThreadViewport
        messages={[
          { role: "assistant", content: "![network](https://example.com/a.png)" },
          { role: "assistant", content: "![local](C:\\workspace\\result.png)" },
        ]}
        loading={false}
        sessionId="session-a"
      />
    );

    expect(view.getByRole("img", { name: "network" }).getAttribute("src"))
      .toBe("https://example.com/a.png");
    expect(view.getByRole("img", { name: "local" }).getAttribute("src"))
      .toBe("/sessions/session-a/local-image?path=C%3A%5Cworkspace%5Cresult.png");
  });

  it("turns image download links into inline message images", () => {
    const view = render(
      <ThreadViewport
        messages={[{
          role: "assistant",
          content: "图片已生成：[点击下载 heart.png](/downloads/dl_demo)",
        }]}
        loading={false}
        sessionId="session-a"
      />
    );

    expect(view.getByRole("img", { name: "heart.png" }).getAttribute("src"))
      .toBe("/downloads/dl_demo");
  });

  it("renders adjacent display math formulas with KaTeX", () => {
    const view = render(
      <ThreadViewport
        messages={[{
          role: "assistant",
          content: "$$x = 16\\sin^3t$$$$y = 13\\cos t - 5\\cos 2t -2\\cos 3t -\\cos 4t$$",
        }]}
        loading={false}
        sessionId="session-math"
      />
    );

    expect(view.container.querySelectorAll(".katex-display")).toHaveLength(2);
    expect(view.container.textContent).toContain("x=16");
    expect(view.container.textContent).toContain("y=13");
  });

  it("renders native LaTeX inline and display delimiters", () => {
    const view = render(
      <ThreadViewport
        messages={[{
          role: "assistant",
          content: "设 \\(D\\) 是有界区域，且 \\(P(x,y)\\) 连续：\n\n\\[\n\\oint_{\\partial D} P\\,dx + Q\\,dy = \\iint_D \\left(\\frac{\\partial Q}{\\partial x} - \\frac{\\partial P}{\\partial y}\\right) dx\\,dy\n\\]",
        }]}
        loading={false}
        sessionId="session-native-latex"
      />
    );

    expect(view.container.querySelectorAll(".katex").length).toBeGreaterThanOrEqual(3);
    expect(
      view.container.querySelectorAll(".katex-display"),
      view.container.innerHTML
    ).toHaveLength(1);

    const visibleMath = Array.from(
      view.container.querySelectorAll<HTMLElement>(".katex-html")
    ).map((node) => node.textContent || "").join("");
    expect(visibleMath).not.toContain("\\oint");
    expect(visibleMath).not.toContain("\\partial");

    const annotations = Array.from(
      view.container.querySelectorAll("annotation[encoding='application/x-tex']")
    ).map((node) => node.textContent || "");
    expect(annotations.some((value) => value.includes("\\oint"))).toBe(true);
  });

  it("shows rollback only for checkpointed user messages when workspace rollback is enabled", () => {
    const onRollback = vi.fn().mockResolvedValue(undefined);
    const view = render(
      <ThreadViewport
        messages={[
          { role: "user", content: "old", messageId: "old" },
          {
            role: "user",
            content: "revertible",
            messageId: "m1",
            rollbackCheckpointId: "cp_1",
            rollbackAvailable: true,
          },
          { role: "assistant", content: "answer", rollbackCheckpointId: "cp_1", rollbackAvailable: true },
        ]}
        loading={false}
        sessionId="session-rollback"
        rollbackEnabled
        onRollback={onRollback}
      />
    );

    const buttons = view.getAllByRole("button", { name: "回退到此消息发送前" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(onRollback).toHaveBeenCalledWith("cp_1");
  });

  it("hides or disables rollback when unavailable or already rolling back", () => {
    const message = {
      role: "user" as const,
      content: "checkpointed",
      rollbackCheckpointId: "cp_1",
      rollbackAvailable: true,
    };
    const hidden = render(
      <ThreadViewport messages={[message]} loading={false} sessionId="s" />
    );
    expect(hidden.queryByRole("button", { name: "回退到此消息发送前" })).toBeNull();
    hidden.unmount();

    const disabled = render(
      <ThreadViewport
        messages={[message]}
        loading={false}
        sessionId="s"
        rollbackEnabled
        rollingBack
      />
    );
    expect(disabled.getByRole("button", { name: "回退到此消息发送前" }))
      .toHaveProperty("disabled", true);
  });
});
