// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AgentSettingsSection } from "./AgentSettingsSection";

const api = vi.hoisted(() => ({
  fetchAgentSettings: vi.fn(),
  saveAgentSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => api);

const settings = {
  backend: "sjtuclaw" as const,
  agents: [
    {
      id: "sjtuclaw" as const,
      name: "SJTUClaw",
      description: "内置 Agent",
      installed: true,
      command: "内置运行时",
      status: "随 SJTUClaw 提供",
    },
    {
      id: "claude" as const,
      name: "Claude Code",
      description: "本机 Claude Code",
      installed: true,
      command: "C:\\tools\\claude.exe",
      status: "已检测到可用命令",
    },
    {
      id: "pi" as const,
      name: "Pi Agent",
      description: "Pi coding agent",
      installed: false,
      command: "",
      status: "找不到可运行的 Pi",
    },
  ],
  piProvider: "",
  piModel: "",
  piThinking: "",
  piTrustTools: false,
  claudeModel: "",
  claudePermissionMode: "default",
  claudeTrustTools: false,
};

beforeEach(() => {
  api.fetchAgentSettings.mockResolvedValue({ ok: true, settings });
  api.saveAgentSettings.mockImplementation(async (request) => ({
    ok: true,
    settings: { ...settings, backend: request.backend },
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Agent settings", () => {
  it("shows detected commands and disables missing agents", async () => {
    const view = render(<AgentSettingsSection />);

    expect(await view.findByText("C:\\tools\\claude.exe")).toBeTruthy();
    expect(view.getAllByText("已安装")).toHaveLength(2);
    expect(view.getByText("未安装")).toBeTruthy();
    expect(view.getByText("找不到可运行的 Pi")).toBeTruthy();

    const claude = view.getByRole("radio", { name: /Claude Code/ });
    expect(claude.querySelector("img")?.getAttribute("src")).toBe("/agent-icons/claude.svg");
    const pi = view.getByRole("radio", { name: /Pi Agent/ }) as HTMLButtonElement;
    expect(pi.querySelector("img")?.getAttribute("src")).toBe("/agent-icons/pi.svg");
    const sjtuclaw = view.getByRole("radio", { name: /SJTUClaw/ });
    expect(sjtuclaw.querySelector("img")?.getAttribute("src")).toBe("/favicon.ico");
    expect(pi.disabled).toBe(true);
    expect(pi.getAttribute("aria-checked")).toBe("false");
  });

  it("selects an installed Agent and saves it as the default backend", async () => {
    const view = render(<AgentSettingsSection />);
    const claude = await view.findByRole("radio", { name: /Claude Code/ });

    fireEvent.click(claude);
    expect(claude.getAttribute("aria-checked")).toBe("true");
    expect(view.getByText("Claude Code 配置")).toBeTruthy();
    fireEvent.click(view.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(api.saveAgentSettings).toHaveBeenCalledWith(
        expect.objectContaining({ backend: "claude" }),
      );
    });
    expect(await view.findByText("已保存；该 Agent 将作为新会话的默认后端")).toBeTruthy();
  });

  it("reruns installation detection on demand", async () => {
    const view = render(<AgentSettingsSection />);
    const refresh = await view.findByRole("button", { name: "重新检测" });

    fireEvent.click(refresh);

    await waitFor(() => expect(api.fetchAgentSettings).toHaveBeenCalledTimes(2));
    expect(await view.findByText("已重新检测本机 Agent")).toBeTruthy();
  });
});
