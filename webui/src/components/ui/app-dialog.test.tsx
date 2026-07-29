// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AppDialogProvider, useAppDialog } from "./app-dialog";

afterEach(() => {
  cleanup();
  delete document.body.dataset.promptResult;
  delete document.body.dataset.confirmResult;
});

function DialogHarness() {
  const { confirmDialog, promptDialog } = useAppDialog();

  return (
    <div>
      <button
        type="button"
        onClick={async () => {
          const value = await promptDialog({
            title: "重命名对话",
            description: "输入一个便于识别的新标题。",
            label: "新标题",
            defaultValue: "旧标题",
            confirmLabel: "保存",
            required: true,
          });
          document.body.dataset.promptResult = value === null ? "cancelled" : value;
        }}
      >
        打开输入框
      </button>
      <button
        type="button"
        onClick={async () => {
          const value = await confirmDialog({
            title: "删除对话",
            description: "确定删除此对话吗？",
            confirmLabel: "删除",
            variant: "destructive",
          });
          document.body.dataset.confirmResult = String(value);
        }}
      >
        打开确认框
      </button>
    </div>
  );
}

describe("AppDialogProvider", () => {
  it("collects text with the themed prompt dialog", async () => {
    const view = render(
      <AppDialogProvider>
        <DialogHarness />
      </AppDialogProvider>,
    );

    fireEvent.click(view.getByRole("button", { name: "打开输入框" }));

    const dialog = view.getByRole("dialog", { name: "重命名对话" });
    expect(dialog.className).toContain("bg-popover");
    expect(dialog.className).toContain("z-[240]");

    const input = view.getByLabelText("新标题");
    expect((input as HTMLInputElement).value).toBe("旧标题");
    fireEvent.change(input, { target: { value: "新的标题" } });
    fireEvent.click(view.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(document.body.dataset.promptResult).toBe("新的标题"));
  });

  it("returns false when a confirmation is cancelled", async () => {
    const view = render(
      <AppDialogProvider>
        <DialogHarness />
      </AppDialogProvider>,
    );

    fireEvent.click(view.getByRole("button", { name: "打开确认框" }));
    expect(view.getByRole("dialog", { name: "删除对话" })).toBeTruthy();
    fireEvent.click(view.getByRole("button", { name: "取消" }));

    await waitFor(() => expect(document.body.dataset.confirmResult).toBe("false"));
  });
});
