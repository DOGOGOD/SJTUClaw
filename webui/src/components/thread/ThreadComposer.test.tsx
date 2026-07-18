// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThreadComposer } from "./ThreadComposer";

afterEach(cleanup);

describe("ThreadComposer keyboard interactions", () => {
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
});
