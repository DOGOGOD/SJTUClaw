import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  RefreshCw,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { fetchAgentSettings, saveAgentSettings } from "@/lib/api";
import type {
  AgentBackend,
  AgentInstallation,
  AgentSettings,
} from "@/lib/types";
import { cn } from "@/lib/utils";

function loadErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "加载失败，请稍后重试";
}

function AgentIcon({ id }: { id: AgentBackend }) {
  if (id === "claude") {
    return (
      <img
        src="/agent-icons/claude.svg"
        alt=""
        aria-hidden="true"
        className="h-full w-full"
      />
    );
  }
  if (id === "pi") {
    return (
      <img
        src="/agent-icons/pi.svg"
        alt=""
        aria-hidden="true"
        className="h-6 w-6"
      />
    );
  }
  return (
    <img
      src="/favicon.ico"
      alt=""
      aria-hidden="true"
      className="h-full w-full object-cover"
    />
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="text-[11px] font-medium text-muted-foreground">{children}</label>;
}

function InstallationStatus({ agent }: { agent: AgentInstallation }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium",
        agent.installed
          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
          : "border-border/70 bg-muted/40 text-muted-foreground",
      )}
    >
      {agent.installed
        ? <CheckCircle2 className="h-3.5 w-3.5" />
        : <AlertCircle className="h-3.5 w-3.5" />}
      {agent.installed ? "已安装" : "未安装"}
    </span>
  );
}

export function AgentSettingsSection() {
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async (refresh = false) => {
    refresh ? setRefreshing(true) : setLoading(true);
    setError("");
    try {
      const data = await fetchAgentSettings();
      setSettings(data.settings);
      if (refresh) {
        setMessage("已重新检测本机 Agent");
        window.setTimeout(() => setMessage(""), 2600);
      }
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const selectedAgent = useMemo(
    () => settings?.agents.find((agent) => agent.id === settings.backend),
    [settings],
  );

  const handleSave = async () => {
    if (!settings) return;
    if (!selectedAgent?.installed) {
      setError("请选择一个已安装的 Agent");
      return;
    }
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const data = await saveAgentSettings({
        backend: settings.backend,
        piProvider: settings.piProvider,
        piModel: settings.piModel,
        piThinking: settings.piThinking,
        piTrustTools: settings.piTrustTools,
        claudeModel: settings.claudeModel,
        claudePermissionMode: settings.claudePermissionMode,
        claudeTrustTools: settings.claudeTrustTools,
      });
      setSettings(data.settings);
      setMessage("已保存；该 Agent 将作为新会话的默认后端");
      window.setTimeout(() => setMessage(""), 3000);
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div aria-label="正在加载 Agent 设置" className="animate-pulse">
        <div className="h-7 w-28 rounded-md bg-muted" />
        <div className="mt-2 h-4 w-96 max-w-full rounded-md bg-muted/70" />
        <div className="mt-7 overflow-hidden rounded-xl border border-border/60">
          {[0, 1, 2].map((item) => (
            <div key={item} className="flex gap-3 border-b border-border/50 p-4 last:border-b-0">
              <div className="h-10 w-10 rounded-lg bg-muted" />
              <div className="flex-1">
                <div className="h-4 w-32 rounded bg-muted" />
                <div className="mt-2 h-3 w-2/3 rounded bg-muted/70" />
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (!settings) {
    return (
      <section>
        <h2 className="text-xl font-semibold tracking-[-0.025em]">Agent 设置</h2>
        <p className="mt-7 text-sm text-destructive" role="alert">加载失败：{error}</p>
        <Button className="mt-3" variant="outline" size="sm" onClick={() => void load()}>
          重新加载
        </Button>
      </section>
    );
  }

  return (
    <section>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold tracking-[-0.025em]">Agent 设置</h2>
          <p className="mt-1.5 max-w-xl text-[13px] leading-relaxed text-muted-foreground">
            检测本机可用的 Agent，并设置新会话默认使用的后端。已有会话仍保持各自的选择。
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="gap-1.5"
          disabled={refreshing || saving}
          onClick={() => void load(true)}
        >
          <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
          {refreshing ? "检测中..." : "重新检测"}
        </Button>
      </div>

      <div className="mt-7">
        <p id="agent-backend-label" className="mb-2 text-[11px] font-medium text-muted-foreground">
          新会话默认 Agent
        </p>
        <div
          role="radiogroup"
          aria-labelledby="agent-backend-label"
          className="overflow-hidden rounded-xl border border-border/60 bg-card/40"
        >
          {settings.agents.map((agent) => {
            const selected = settings.backend === agent.id;
            return (
              <button
                key={agent.id}
                type="button"
                role="radio"
                aria-checked={selected}
                aria-disabled={!agent.installed}
                disabled={!agent.installed}
                onClick={() => setSettings({ ...settings, backend: agent.id })}
                className={cn(
                  "flex w-full items-start gap-3 border-b border-border/50 p-4 text-left transition-colors last:border-b-0",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                  agent.installed && "hover:bg-muted/35 active:bg-muted/55",
                  selected && "bg-muted/45",
                  !agent.installed && "cursor-not-allowed opacity-70",
                )}
              >
                <span
                  className={cn(
                    "mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border",
                    agent.id === "claude" && "overflow-hidden bg-background",
                    agent.id === "pi" && "border-black bg-black",
                    agent.id === "sjtuclaw" && "overflow-hidden border-border/70 bg-background",
                    selected && "ring-2 ring-foreground/20 ring-offset-1 ring-offset-background",
                  )}
                >
                  <AgentIcon id={agent.id} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">{agent.name}</span>
                    {selected && (
                      <span className="rounded-md bg-foreground/10 px-1.5 py-0.5 text-[10px] font-medium text-foreground">
                        当前默认
                      </span>
                    )}
                  </span>
                  <span className="mt-1 block text-[12px] leading-relaxed text-muted-foreground">
                    {agent.description}
                  </span>
                  <span
                    className={cn(
                      "mt-1.5 block break-all text-[11px] leading-relaxed",
                      agent.installed ? "font-mono text-muted-foreground/80" : "text-muted-foreground",
                    )}
                  >
                    {agent.installed ? agent.command : agent.status}
                  </span>
                </span>
                <InstallationStatus agent={agent} />
              </button>
            );
          })}
        </div>
      </div>

      {settings.backend === "sjtuclaw" && (
        <div className="mt-4 rounded-xl border border-border/60 bg-muted/20 px-4 py-3 text-[12px] leading-relaxed text-muted-foreground">
          内置 Agent 使用 LLM 模块中的 API、模型和上下文配置。
        </div>
      )}

      {settings.backend === "pi" && (
        <div className="mt-4 rounded-xl border border-border/60 bg-card/40 p-4">
          <p className="text-sm font-semibold">Pi Agent 配置</p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <div>
              <FieldLabel>Pi provider（可选）</FieldLabel>
              <Input
                className="mt-1"
                value={settings.piProvider}
                onChange={(event) => setSettings({ ...settings, piProvider: event.target.value })}
                placeholder="留空则复用 LLM 或 Pi auth"
              />
            </div>
            <div>
              <FieldLabel>Pi model（可选）</FieldLabel>
              <Input
                className="mt-1"
                value={settings.piModel}
                onChange={(event) => setSettings({ ...settings, piModel: event.target.value })}
                placeholder="provider 原生模型 ID"
              />
            </div>
            <div>
              <FieldLabel>Pi thinking</FieldLabel>
              <select
                className="mt-1 h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={settings.piThinking}
                onChange={(event) => setSettings({ ...settings, piThinking: event.target.value })}
              >
                <option value="">使用 Pi 默认值</option>
                {["off", "minimal", "low", "medium", "high", "xhigh", "max"].map((level) => (
                  <option key={level} value={level}>{level}</option>
                ))}
              </select>
            </div>
            <label className="flex items-center gap-2 self-end pb-2 text-sm">
              <Checkbox
                checked={settings.piTrustTools}
                onChange={(event) => setSettings({ ...settings, piTrustTools: event.target.checked })}
              />
              信任 Pi 的写入和 Shell 工具（跳过审批）
            </label>
          </div>
        </div>
      )}

      {settings.backend === "claude" && (
        <div className="mt-4 rounded-xl border border-border/60 bg-card/40 p-4">
          <p className="text-sm font-semibold">Claude Code 配置</p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <div>
              <FieldLabel>Claude model（可选）</FieldLabel>
              <Input
                className="mt-1"
                value={settings.claudeModel}
                onChange={(event) => setSettings({ ...settings, claudeModel: event.target.value })}
                placeholder="留空则沿用 Claude Code 默认模型"
              />
            </div>
            <div>
              <FieldLabel>Claude permission mode</FieldLabel>
              <select
                className="mt-1 h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={settings.claudePermissionMode}
                onChange={(event) => setSettings({ ...settings, claudePermissionMode: event.target.value })}
              >
                <option value="default">default（推荐，危险操作需审批）</option>
                <option value="acceptEdits">acceptEdits（编辑仍需 SJTUClaw 审批）</option>
                <option value="plan">plan</option>
                <option value="dontAsk">dontAsk</option>
                <option value="auto">auto</option>
              </select>
            </div>
            <label className="flex items-center gap-2 text-sm md:col-span-2">
              <Checkbox
                checked={settings.claudeTrustTools}
                onChange={(event) => setSettings({ ...settings, claudeTrustTools: event.target.checked })}
              />
              信任 Claude Code 的所有工具（跳过 SJTUClaw 与 Claude Code 审批，仅限可信环境）
            </label>
            <p className="text-xs leading-relaxed text-muted-foreground md:col-span-2">
              搜索、读取和查询无需 SJTUClaw 审批；写入、删除及其他会改变状态的操作仍会请求确认。
            </p>
          </div>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          onClick={() => void handleSave()}
          disabled={saving || !selectedAgent?.installed}
        >
          {saving ? "保存中..." : "保存"}
        </Button>
        {message && <span className="text-xs text-muted-foreground" role="status">{message}</span>}
        {error && <span className="text-xs text-destructive" role="alert">{error}</span>}
      </div>
    </section>
  );
}
