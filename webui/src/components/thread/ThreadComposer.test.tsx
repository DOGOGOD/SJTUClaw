// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThreadComposer } from "./ThreadComposer";
import * as api from "@/lib/api";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ThreadComposer keyboard interactions", () => {
  it("uses the Claw chat placeholder in both composer modes", () => {
    const home = render(
      <ThreadComposer onSend={vi.fn().mockResolvedValue(undefined)} sessionId="session-a" home />
    );
    expect(home.getByPlaceholderText("Chat or Work With Claw")).toBeTruthy();
    home.unmount();

    const thread = render(
      <ThreadComposer onSend={vi.fn().mockResolvedValue(undefined)} sessionId="session-a" />
    );
    expect(thread.getByPlaceholderText("Chat or Work With Claw")).toBeTruthy();
  });

  it("refreshes the workspace indicator after a slash workspace command", async () => {
    const fetchWorkspace = vi.spyOn(api, "fetchWorkspace").mockResolvedValue({
      ok: true,
      sessionId: "session-a",
      workspace: "C:\\project-one",
      isSet: true,
    });
    const view = render(
      <ThreadComposer onSend={vi.fn().mockResolvedValue(undefined)} sessionId="session-a" workspaceRefreshToken={0} />
    );
    await waitFor(() => expect(fetchWorkspace).toHaveBeenCalledTimes(1));

    fetchWorkspace.mockResolvedValue({
      ok: true,
      sessionId: "session-a",
      workspace: "C:\\project-two",
      isSet: true,
    });
    view.rerender(
      <ThreadComposer onSend={vi.fn().mockResolvedValue(undefined)} sessionId="session-a" workspaceRefreshToken={1} />
    );
    await waitFor(() => expect(fetchWorkspace).toHaveBeenCalledTimes(2));
    expect(view.getByTitle("project-two")).toBeTruthy();
  });

  it("clears immediately after clicking send", () => {
    const onSend = vi.fn(() => new Promise<void>(() => {}));
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "  hello  " } });
    fireEvent.click(view.getByTitle("发送"));

    expect(onSend).toHaveBeenCalledWith("hello");
    expect(composer.value).toBe("");
  });

  it("sends with Enter and inserts a newline with Shift+Enter", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "first line" } });
    fireEvent.keyDown(composer, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(composer, { target: { value: "first line\nsecond line" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("first line\nsecond line");
    expect(composer.value).toBe("");
  });

  it("navigates sent messages newest-first and restores the current draft", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "first" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    fireEvent.change(composer, { target: { value: "second" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    fireEvent.change(composer, { target: { value: "draft" } });

    fireEvent.keyDown(composer, { key: "ArrowUp" });
    expect(composer.value).toBe("second");
    fireEvent.keyDown(composer, { key: "ArrowUp" });
    expect(composer.value).toBe("first");
    fireEvent.keyDown(composer, { key: "ArrowDown" });
    expect(composer.value).toBe("second");
    fireEvent.keyDown(composer, { key: "ArrowDown" });
    expect(composer.value).toBe("draft");

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
  });

  it("loads previously sent messages supplied by the current session", () => {
    const view = render(
      <ThreadComposer
        onSend={vi.fn().mockResolvedValue(undefined)}
        sessionId="session-a"
        messageHistory={["older", "newer"]}
      />
    );
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.keyDown(composer, { key: "ArrowUp" });
    expect(composer.value).toBe("newer");
    fireEvent.keyDown(composer, { key: "ArrowUp" });
    expect(composer.value).toBe("older");
  });

  it("restores a failed message, reports the error, and excludes it from history", async () => {
    const onSend = vi.fn().mockRejectedValue(new Error("网络不可用"));
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "retry me" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(composer.value).toBe("");

    await waitFor(() => expect(composer.value).toBe("retry me"));
    expect(view.getByRole("alert").textContent).toBe("网络不可用");

    fireEvent.change(composer, { target: { value: "" } });
    fireEvent.keyDown(composer, { key: "ArrowUp" });
    expect(composer.value).toBe("");
  });

  it("does not send while an input method is composing text", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    fireEvent.change(composer, { target: { value: "中文" } });
    fireEvent.keyDown(composer, { key: "Enter", isComposing: true });

    expect(onSend).not.toHaveBeenCalled();
    expect(composer.value).toBe("中文");
  });

  it("keeps a pasted image pending until text is entered and the message is sent", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const view = render(
      <ThreadComposer
        onSend={onSend}
        sessionId="session-a"
      />
    );
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;
    const image = new File(["png"], "clipboard.png", { type: "image/png" });

    fireEvent.paste(composer, {
      clipboardData: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
        files: [image],
      },
    });

    expect(onSend).not.toHaveBeenCalled();
    expect(view.getByLabelText("待发送图片")).toBeTruthy();
    expect(composer.value).toBe("");

    fireEvent.change(composer, { target: { value: "请描述这张图" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("请描述这张图", [image]));
    expect(view.queryByLabelText("待发送图片")).toBeNull();
  });

  it("keeps normal text paste behavior when the clipboard has no image", () => {
    const view = render(
      <ThreadComposer
        onSend={vi.fn().mockResolvedValue(undefined)}
        sessionId="session-a"
      />
    );
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;

    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", {
      value: { items: [], files: [] },
    });
    composer.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
  });

  it("can send a pasted image without additional text by clicking send", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;
    const image = new File(["png"], "clipboard.png", { type: "image/png" });

    fireEvent.paste(composer, {
      clipboardData: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
        files: [image],
      },
    });
    fireEvent.click(view.getByTitle("发送"));

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("", [image]));
  });

  it("restores both text and the pending image when combined sending fails", async () => {
    const onSend = vi.fn().mockRejectedValue(new Error("图片发送失败"));
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;
    const image = new File(["png"], "clipboard.png", { type: "image/png" });

    fireEvent.paste(composer, {
      clipboardData: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
        files: [image],
      },
    });
    fireEvent.change(composer, { target: { value: "和图片一起发送" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    await waitFor(() => expect(view.getByRole("alert").textContent).toBe("图片发送失败"));
    expect(composer.value).toBe("和图片一起发送");
    expect(view.getByLabelText("待发送图片")).toBeTruthy();
  });

  it("removes a pending image without sending it", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const view = render(<ThreadComposer onSend={onSend} sessionId="session-a" />);
    const composer = view.getByRole("textbox") as HTMLTextAreaElement;
    const image = new File(["png"], "clipboard.png", { type: "image/png" });

    fireEvent.paste(composer, {
      clipboardData: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
        files: [image],
      },
    });
    fireEvent.click(view.getByLabelText("移除图片 1"));
    fireEvent.keyDown(composer, { key: "Enter" });

    expect(view.queryByLabelText("待发送图片")).toBeNull();
    expect(onSend).not.toHaveBeenCalled();
  });
});
