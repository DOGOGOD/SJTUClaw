import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Bot,
  Brain,
  CheckCircle2,
  Clock,
  Database,
  Eye,
  EyeOff,
  FileText,
  Moon,
  PackagePlus,
  PawPrint,
  QrCode,
  RadioTower,
  Sun,
  Trash2,
  Upload,
  Wrench,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/input";
import { cn, formatTime } from "@/lib/utils";
import {
  fetchSystemPrompt, saveSystemPrompt,
  fetchSoul, saveSoul,
  fetchMemories, addMemory, deleteMemory,
  fetchCronJobs, createCronJob, deleteCronJob, disableCronJob, enableCronJob,
  fetchSkills,
  fetchSessions,
  fetchChannelSettings, saveQQChannelSettings, startQQOnboard, pollQQOnboard,
  fetchLLMSettings, saveLLMSettings,
  uploadSkillPackage, removeSkill,
} from "@/lib/api";
import type {
  SettingsSection,
  MemoryEntry,
  CronJobInfo,
  SkillInfo,
  SessionSummary,
  QQChannelSettings,
  QQConnectionStatus,
  LLMSettings,
} from "@/lib/types";
import { PetSettingsSection } from "@/components/settings/PetSettingsSection";

interface SettingsViewProps {
  theme: "light" | "dark";
  activeSection: SettingsSection;
  onToggleTheme: () => void;
  onBackToChat: () => void;
  onSectionChange: (section: SettingsSection) => void;
  activeSessionId?: string | null;
}

const SECTIONS: { key: SettingsSection; label: string; Icon: typeof FileText }[] = [
  { key: "prompt", label: "System Prompt", Icon: FileText },
  { key: "soul", label: "Soul", Icon: Brain },
  { key: "memory", label: "Memory", Icon: Database },
  { key: "channel", label: "Channel", Icon: RadioTower },
  { key: "llm", label: "LLM", Icon: Bot },
  { key: "cron", label: "Cron Jobs", Icon: Clock },
  { key: "skills", label: "Skills", Icon: Wrench },
  { key: "pet", label: "Pet", Icon: PawPrint },
];

export function SettingsView({
  theme, activeSection, onToggleTheme, onBackToChat, onSectionChange, activeSessionId,
}: SettingsViewProps) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-background md:flex-row">
      {/* Settings sidebar */}
      <nav className="flex shrink-0 border-b border-border/60 bg-sidebar/70 md:w-56 md:flex-col md:border-b-0 md:border-r">
        <div className="flex h-14 shrink-0 items-center gap-2 border-r border-border/40 px-3 md:border-b md:border-r-0 md:px-4">
          <Button variant="ghost" size="icon-sm" onClick={onBackToChat} title="返回">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <span className="text-sm font-medium">设置</span>
        </div>
        <div className="flex flex-1 gap-1 overflow-x-auto p-2 md:flex-col md:overflow-visible">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              onClick={() => onSectionChange(s.key)}
              className={cn(
                "flex shrink-0 items-center gap-2.5 rounded-xl px-3 py-2 text-[13px] text-left transition-colors duration-150",
                activeSection === s.key
                  ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
                  : "text-muted-foreground hover:text-foreground hover:bg-sidebar-accent/50"
              )}
            >
              <s.Icon className="h-3.5 w-3.5" />
              <span>{s.label}</span>
            </button>
          ))}
        </div>
        <div className="hidden border-t border-border/40 p-2 md:block">
          <button
            onClick={onToggleTheme}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition-colors duration-150 text-muted-foreground hover:text-foreground hover:bg-sidebar-accent/50"
          >
            {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
            <span>{theme === "dark" ? "浅色模式" : "深色模式"}</span>
          </button>
        </div>
      </nav>

      {/* Content panel */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl p-5 md:p-10">
          {activeSection === "prompt" && <SystemPromptSection />}
          {activeSection === "soul" && <SoulSection />}
          {activeSection === "memory" && <MemorySection />}
          {activeSection === "channel" && <ChannelSection />}
          {activeSection === "llm" && <LLMSection />}
          {activeSection === "cron" && <CronSection activeSessionId={activeSessionId} />}
          {activeSection === "skills" && <SkillsSection />}
          {activeSection === "pet" && <PetSettingsSection />}
        </div>
      </div>
    </div>
  );
}

/* --- Section wrapper --- */
function Section({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <div>
      <h2 className="text-xl font-semibold tracking-[-0.025em]">{title}</h2>
      {desc && <p className="mt-1.5 max-w-xl text-[13px] leading-relaxed text-muted-foreground">{desc}</p>}
      <div className="mt-7">{children}</div>
    </div>
  );
}

function loadErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "加载失败，请稍后重试";
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="text-[11px] font-medium text-muted-foreground">{children}</label>;
}

function ToggleSwitch({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={checked}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-6 w-11 shrink-0 overflow-hidden rounded-full border transition-colors disabled:opacity-40",
        checked ? "border-foreground bg-foreground" : "border-border/80 bg-muted/50"
      )}
    >
      <span
        className={cn(
          "pointer-events-none absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-background shadow-sm transition-transform",
          checked ? "translate-x-5" : "translate-x-0"
        )}
      />
    </button>
  );
}

function StatusPill({ running, text }: { running: boolean; text: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium",
        running
          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          : "border-border/70 bg-muted/40 text-muted-foreground"
      )}
    >
      {running ? <CheckCircle2 className="h-3.5 w-3.5" /> : <AlertCircle className="h-3.5 w-3.5" />}
      {text}
    </span>
  );
}

/* --- Channel settings --- */
function ChannelSection() {
  const [qq, setQq] = useState<QQChannelSettings | null>(null);
  const [status, setStatus] = useState<QQConnectionStatus | null>(null);
  const [clientSecret, setClientSecret] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [qrOpen, setQrOpen] = useState(false);
  const [qrImage, setQrImage] = useState("");
  const [qrTaskId, setQrTaskId] = useState("");
  const [qrStatus, setQrStatus] = useState("");

  const statusText = (value: QQConnectionStatus | null) => {
    if (!value) return "";
    if (value.running) return "已连接";
    if (value.starting) return "连接中";
    if (value.enabled && value.configured) return "已启用";
    if (value.enabled && !value.configured) return "未配置";
    return "已关闭";
  };

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchChannelSettings();
      setQq(data.settings.qq);
      setStatus(data.status);
      setClientSecret("");
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!qrOpen || !qrTaskId) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const data = await pollQQOnboard(qrTaskId);
        if (cancelled) return;
        if (data.status === "completed") {
          if (data.settings?.qq) setQq(data.settings.qq);
          if (data.connection) setStatus(data.connection);
          setQrOpen(false);
          setQrStatus("扫码成功，QQ 通道已更新");
          setMessage("QQ 扫码连接成功");
          setClientSecret("");
        } else if (data.status === "expired") {
          setQrStatus(data.message || "二维码已过期");
        }
      } catch {}
    }, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [qrOpen, qrTaskId]);

  const saveQQ = async (nextQq: QQChannelSettings, nextSecret = clientSecret, successText = "已保存，服务配置已动态应用") => {
    setSaving(true);
    setMessage("");
    setError("");
    if (nextQq.enabled && !nextQq.appId.trim()) {
      setError("启用 QQ 通道前请填写 QQ_APP_ID");
      setSaving(false);
      return;
    }
    try {
      const data = await saveQQChannelSettings({
        enabled: nextQq.enabled,
        appId: nextQq.appId.trim(),
        clientSecret: nextSecret.trim() || undefined,
        allowFrom: nextQq.allowFrom.trim(),
        msgFormat: nextQq.msgFormat,
        ackMessage: nextQq.ackMessage,
      });
      setQq(data.settings.qq);
      setStatus(data.status);
      setClientSecret("");
      setMessage(successText);
      setTimeout(() => setMessage(""), 2600);
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    if (!qq) return;
    await saveQQ(qq);
  };

  const handleToggle = (enabled: boolean) => {
    if (!qq) return;
    const nextQq = { ...qq, enabled };
    setQq(nextQq);
    setStatus(status ? { ...status, enabled, starting: enabled && status.configured && !status.running, message: enabled ? "QQ 通道连接中" : "QQ 通道未启用" } : status);
    void saveQQ(nextQq, "", enabled ? "QQ 通道已启用，正在连接" : "QQ 通道已关闭");
  };

  const handleStartQr = async () => {
    setQrStatus("正在创建扫码任务...");
    setQrOpen(true);
    setQrImage("");
    try {
      const data = await startQQOnboard();
      setQrTaskId(data.taskId);
      setQrImage(data.qrImage);
      setQrStatus("等待手机 QQ 扫码确认");
    } catch (err) {
      setQrStatus(loadErrorMessage(err));
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;
  if (!qq) return (
    <Section title="Channel 设置" desc="配置消息渠道连接参数。">
      <p className="text-sm text-destructive" role="alert">加载失败：{error}</p>
      <Button className="mt-3" variant="outline" size="sm" onClick={() => void load()}>重新加载</Button>
    </Section>
  );

  return (
    <Section title="Channel 设置" desc="当前支持 QQ 渠道，可保存配置后即时重启连接。">
      <div className="rounded-xl border border-border/60 bg-card/40 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/40 pb-4">
          <div>
            <p className="text-sm font-semibold">QQ 渠道</p>
            <p className="mt-1 text-[12px] text-muted-foreground">关联 QQ_ENABLED、QQ_APP_ID、QQ_CLIENT_SECRET 等配置。</p>
          </div>
          <div className="flex items-center gap-3">
            {status && <StatusPill running={status.running} text={statusText(status)} />}
            <ToggleSwitch checked={qq.enabled} onChange={handleToggle} disabled={saving} />
          </div>
        </div>

        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <div>
            <FieldLabel>QQ_APP_ID</FieldLabel>
            <Input className="mt-1" value={qq.appId} onChange={(e) => setQq({ ...qq, appId: e.target.value })} placeholder="请输入 AppID" />
          </div>
          <div>
            <FieldLabel>QQ_CLIENT_SECRET</FieldLabel>
            <Input className="mt-1" type="password" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} placeholder={qq.clientSecretMasked || "留空则不修改"} />
          </div>
          <div>
            <FieldLabel>QQ_ALLOW_FROM</FieldLabel>
            <Input className="mt-1" value={qq.allowFrom} onChange={(e) => setQq({ ...qq, allowFrom: e.target.value })} placeholder="openid1,openid2 或 *" />
          </div>
          <div>
            <FieldLabel>QQ_MSG_FORMAT</FieldLabel>
            <select value={qq.msgFormat} onChange={(e) => setQq({ ...qq, msgFormat: e.target.value as "markdown" | "text" })} className="mt-1 h-9 w-full rounded-lg border border-border/80 bg-transparent px-3 text-sm outline-none focus:border-primary/50">
              <option value="markdown">markdown</option>
              <option value="text">text</option>
            </select>
          </div>
          <div className="md:col-span-2">
            <FieldLabel>QQ_ACK_MESSAGE</FieldLabel>
            <Input className="mt-1" value={qq.ackMessage} onChange={(e) => setQq({ ...qq, ackMessage: e.target.value })} placeholder="收到消息后的可选确认提示" />
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={handleSave} disabled={saving}>{saving ? "保存中..." : "保存"}</Button>
          <Button size="sm" variant="outline" onClick={() => void handleStartQr()} className="gap-1.5">
            <QrCode className="h-3.5 w-3.5" />
            扫码连接
          </Button>
          {message && <span className="text-xs text-muted-foreground">{message}</span>}
          {error && <span className="text-xs text-destructive" role="alert">{error}</span>}
        </div>
      </div>

      {qrOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/55 p-4 backdrop-blur-md">
          <div className="w-full max-w-sm rounded-xl border border-border/70 bg-card p-5 shadow-xl">
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold">扫码连接 QQ</p>
              <Button variant="ghost" size="icon-sm" onClick={() => setQrOpen(false)} title="关闭">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="mt-5 flex aspect-square items-center justify-center rounded-lg border border-border/60 bg-background">
              {qrImage ? <img src={qrImage} alt="QQ 扫码授权二维码" className="h-[82%] w-[82%]" /> : <span className="text-xs text-muted-foreground">二维码生成中...</span>}
            </div>
            <p className="mt-4 text-center text-sm font-medium">请使用 QQ 扫码进行授权连接</p>
            <p className="mt-1 text-center text-xs text-muted-foreground">{qrStatus}</p>
          </div>
        </div>
      )}
    </Section>
  );
}

/* --- LLM settings --- */
function LLMSection() {
  const [settings, setSettings] = useState<LLMSettings | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchLLMSettings();
      setSettings(data.settings);
      setApiKey("");
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const validate = () => {
    if (!settings) return "配置未加载";
    if (settings.baseUrl.trim()) {
      try {
        const url = new URL(settings.baseUrl);
        if (!["http:", "https:"].includes(url.protocol)) return "Base_url 必须使用 http 或 https";
      } catch {
        return "Base_url 必须是完整 URL";
      }
    }
    if (settings.backend !== "pi" && !settings.model.trim()) return "请填写模型名称";
    if (!Number.isFinite(settings.contextWindow) || settings.contextWindow < 1024) return "Context window 不能小于 1024";
    if (settings.contextUsageRatio <= 0 || settings.contextUsageRatio > 1) return "Context usage ratio 必须在 0 到 1 之间";
    if (settings.maxOutputTokens < 1) return "Max output tokens 必须大于 0";
    return "";
  };

  const handleSave = async () => {
    if (!settings) return;
    const validation = validate();
    if (validation) {
      setError(validation);
      return;
    }
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const data = await saveLLMSettings({
        ...settings,
        apiKey: apiKey.trim() || undefined,
      });
      setSettings(data.settings);
      setApiKey("");
      setMessage("已保存，新的 LLM 配置已动态应用");
      setTimeout(() => setMessage(""), 2600);
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;
  if (!settings) return (
    <Section title="LLM 设置" desc="配置大语言模型连接参数。">
      <p className="text-sm text-destructive" role="alert">加载失败：{error}</p>
      <Button className="mt-3" variant="outline" size="sm" onClick={() => void load()}>重新加载</Button>
    </Section>
  );

  return (
    <Section title="Agent 与 LLM 设置" desc="选择 SJTUClaw 或 Pi Agent；保存后后续请求立即使用新后端。">
      <div className="rounded-xl border border-border/60 bg-card/40 p-4">
        <div className="grid gap-3">
          <div>
            <FieldLabel>Agent backend</FieldLabel>
            <select
              className="mt-1 h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
              value={settings.backend}
              onChange={(e) => setSettings({ ...settings, backend: e.target.value as "sjtuclaw" | "pi" })}
            >
              <option value="sjtuclaw">SJTUClaw 内置 Agent</option>
              <option value="pi">Pi Agent</option>
            </select>
          </div>
          {settings.backend === "pi" && (
            <div className="grid gap-3 rounded-lg border border-border/50 bg-background/40 p-3 md:grid-cols-2">
              <div>
                <FieldLabel>Pi provider（可选）</FieldLabel>
                <Input className="mt-1" value={settings.piProvider} onChange={(e) => setSettings({ ...settings, piProvider: e.target.value })} placeholder="留空则复用下方 LLM 或 Pi auth" />
              </div>
              <div>
                <FieldLabel>Pi model（可选）</FieldLabel>
                <Input className="mt-1" value={settings.piModel} onChange={(e) => setSettings({ ...settings, piModel: e.target.value })} placeholder="provider 原生模型 ID" />
              </div>
              <div>
                <FieldLabel>Pi thinking</FieldLabel>
                <select
                  className="mt-1 h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={settings.piThinking}
                  onChange={(e) => setSettings({ ...settings, piThinking: e.target.value })}
                >
                  <option value="">使用 Pi 默认值</option>
                  {['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((level) => <option key={level} value={level}>{level}</option>)}
                </select>
              </div>
              <label className="flex items-center gap-2 self-end pb-2 text-sm">
                <input type="checkbox" checked={settings.piTrustTools} onChange={(e) => setSettings({ ...settings, piTrustTools: e.target.checked })} />
                信任 Pi 的写入和 Shell 工具（跳过审批）
              </label>
            </div>
          )}
          <div>
            <FieldLabel>Base_url</FieldLabel>
            <Input className="mt-1" value={settings.baseUrl} onChange={(e) => setSettings({ ...settings, baseUrl: e.target.value })} placeholder="https://api.example.com/v1" />
          </div>
          <div>
            <FieldLabel>API Key</FieldLabel>
            <div className="mt-1 flex gap-2">
              <Input
                className="api-key-input"
                type={showKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={settings.apiKeyConfigured ? "已配置，留空则保持不变" : "请输入 API Key"}
                autoComplete="new-password"
              />
              <Button type="button" variant="outline" size="icon" onClick={() => setShowKey(!showKey)} title={showKey ? "隐藏 API Key" : "显示 API Key"}>
                {showKey ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
              </Button>
            </div>
          </div>
          <div>
            <FieldLabel>Model</FieldLabel>
            <Input className="mt-1" value={settings.model} onChange={(e) => setSettings({ ...settings, model: e.target.value })} placeholder="gpt-4.1 或兼容模型名称" />
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <FieldLabel>LLM_CONTEXT_WINDOW</FieldLabel>
              <Input className="mt-1" type="number" min={1024} value={settings.contextWindow} onChange={(e) => setSettings({ ...settings, contextWindow: Number(e.target.value) })} />
            </div>
            <div>
              <FieldLabel>LLM_CONTEXT_USAGE_RATIO</FieldLabel>
              <Input className="mt-1" type="number" min={0.1} max={1} step={0.05} value={settings.contextUsageRatio} onChange={(e) => setSettings({ ...settings, contextUsageRatio: Number(e.target.value) })} />
            </div>
            <div>
              <FieldLabel>LLM_MAX_OUTPUT_TOKENS</FieldLabel>
              <Input className="mt-1" type="number" min={1} value={settings.maxOutputTokens} onChange={(e) => setSettings({ ...settings, maxOutputTokens: Number(e.target.value) })} />
            </div>
            <div>
              <FieldLabel>LLM_CONSOLIDATION_RATIO</FieldLabel>
              <Input className="mt-1" type="number" min={0.1} max={1} step={0.05} value={settings.consolidationRatio} onChange={(e) => setSettings({ ...settings, consolidationRatio: Number(e.target.value) })} />
            </div>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button size="sm" onClick={handleSave} disabled={saving}>{saving ? "保存中..." : "保存"}</Button>
          {message && <span className="text-xs text-muted-foreground">{message}</span>}
          {error && <span className="text-xs text-destructive" role="alert">{error}</span>}
        </div>
      </div>
    </Section>
  );
}

/* --- System Prompt --- */
function SystemPromptSection() {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const data = await fetchSystemPrompt();
      setContent(data.content || "");
    } catch (error) {
      setLoadError(loadErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const handleSave = async () => { setSaving(true); try { await saveSystemPrompt(content); setStatus("已保存"); setTimeout(() => setStatus(""), 2000); } catch { setStatus("保存失败"); } finally { setSaving(false); } };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;
  if (loadError) return (
    <Section title="System Prompt" desc="定义 Agent 的核心行为规则。">
      <p className="text-sm text-destructive" role="alert">加载失败：{loadError}</p>
      <Button className="mt-3" variant="outline" size="sm" onClick={() => void load()}>重新加载</Button>
    </Section>
  );

  return (
    <Section title="System Prompt" desc="定义 Agent 的核心行为规则。">
      <Textarea value={content} onChange={(e) => setContent(e.target.value)} className="min-h-[300px]" />
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={handleSave} disabled={saving} size="sm">{saving ? "保存中..." : "保存"}</Button>
        {status && <span className="text-xs text-muted-foreground">{status}</span>}
      </div>
    </Section>
  );
}

/* --- Soul --- */
function SoulSection() {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const data = await fetchSoul();
      setContent(data.content || "");
    } catch (error) {
      setLoadError(loadErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const handleSave = async () => { setSaving(true); try { await saveSoul(content); setStatus("已保存"); setTimeout(() => setStatus(""), 2000); } catch { setStatus("保存失败"); } finally { setSaving(false); } };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;
  if (loadError) return (
    <Section title="Soul" desc="定义 Agent 的人格和交互风格。">
      <p className="text-sm text-destructive" role="alert">加载失败：{loadError}</p>
      <Button className="mt-3" variant="outline" size="sm" onClick={() => void load()}>重新加载</Button>
    </Section>
  );

  return (
    <Section title="Soul" desc="定义 Agent 的人格和交互风格。">
      <Textarea value={content} onChange={(e) => setContent(e.target.value)} className="min-h-[300px]" />
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={handleSave} disabled={saving} size="sm">{saving ? "保存中..." : "保存"}</Button>
        {status && <span className="text-xs text-muted-foreground">{status}</span>}
      </div>
    </Section>
  );
}

/* --- Memory --- */
function MemorySection() {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const data = await fetchMemories();
      setMemories(data.memories || []);
    } catch (error) {
      setLoadError(loadErrorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const handleAdd = async () => { if (!input.trim()) return; try { await addMemory({ content: input.trim() }); setInput(""); refresh(); } catch {} };
  const handleDelete = async (id: string) => { if (!confirm("确定删除？")) return; try { await deleteMemory(id); refresh(); } catch {} };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;
  if (loadError) return (
    <Section title="Memory" desc="长期记忆，跨 session 持久化。">
      <p className="text-sm text-destructive" role="alert">加载失败：{loadError}</p>
      <Button className="mt-3" variant="outline" size="sm" onClick={() => void refresh()}>重新加载</Button>
    </Section>
  );

  return (
    <Section title="Memory" desc="长期记忆，跨 session 持久化。">
      <div className="flex gap-2 mb-4">
        <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder="输入新的记忆内容..." onKeyDown={(e) => e.key === "Enter" && handleAdd()} />
        <Button onClick={handleAdd} size="sm" className="shrink-0">添加</Button>
      </div>
      {memories.length === 0 && <p className="text-sm text-muted-foreground/60">暂无长期记忆</p>}
      {memories.map((m) => (
        <div key={m.id} className="flex items-start gap-3 py-3 border-b border-border/40">
          <div className="flex-1 min-w-0">
            <p className="text-sm leading-relaxed">{m.content}</p>
            <p className="mt-1 text-[10px] text-muted-foreground/50">{m.id}　{m.category || "general"}　{formatTime(m.createdAt)}</p>
          </div>
          <Button variant="ghost" size="sm" className="h-6 text-[10px] text-destructive hover:bg-destructive/10 shrink-0" onClick={() => handleDelete(m.id)}>删除</Button>
        </div>
      ))}
    </Section>
  );
}

/* --- Cron Jobs --- */
function CronSection({ activeSessionId }: { activeSessionId?: string | null }) {
  const [jobs, setJobs] = useState<CronJobInfo[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [name, setName] = useState(""); const [message, setMessage] = useState("");
  const [scheduleKind, setScheduleKind] = useState<"every" | "cron" | "at">("every");
  const [everySeconds, setEverySeconds] = useState(""); const [cronExpr, setCronExpr] = useState("");
  const [tz, setTz] = useState(""); const [at, setAt] = useState("");
  const [targetSessionId, setTargetSessionId] = useState("");
  const [loading, setLoading] = useState(true); const [status, setStatus] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [jd, sd] = await Promise.all([fetchCronJobs(), fetchSessions()]);
      setJobs(jd.jobs || []);
      setSessions(sd.sessions || []);
      // Default to active session, fallback to first available
      if (!targetSessionId) {
        setTargetSessionId(activeSessionId || sd.sessions?.[0]?.sessionId || "default");
      }
    } catch {} finally { setLoading(false); }
  }, [activeSessionId]);
  useEffect(() => { refresh(); }, [refresh]);

  const handleCreate = async () => {
    if (!message.trim()) { setStatus("请填写消息内容"); return; }
    try {
      const sid = targetSessionId || "default";
      const payload: Record<string, unknown> = { message: message.trim(), sessionId: sid };
      if (name.trim()) payload.name = name.trim();
      if (scheduleKind === "every") payload.everySeconds = parseInt(everySeconds) || 3600;
      else if (scheduleKind === "cron") {
        if (!cronExpr.trim()) { setStatus("请填写 cron 表达式"); return; }
        payload.cronExpr = cronExpr.trim();
        if (tz.trim()) payload.tz = tz.trim();
      }
      else {
        if (!at.trim()) { setStatus("请填写 ISO 时间"); return; }
        payload.at = at.trim();
        if (tz.trim()) payload.tz = tz.trim();
      }
      const d = await createCronJob(payload as any);
      if (d.ok) { setStatus(`已为 session ${sid} 创建定时任务`); setMessage(""); setName(""); setEverySeconds(""); setCronExpr(""); setAt(""); refresh(); } else setStatus("创建失败");
    } catch (e: any) { setStatus(e.message || "创建失败"); }
  };

  const _formatMs = (ms: number | null) => { if (ms == null) return "-"; const d = new Date(ms); return isNaN(d.getTime()) ? String(ms) : d.toLocaleString(); };
  const _label = (j: CronJobInfo) => { const s = j.schedule; if (s.kind === "every" && s.everyMs) { const sec = s.everyMs / 1000; return sec >= 3600 ? `每 ${sec / 3600}h` : sec >= 60 ? `每 ${sec / 60}m` : `每 ${sec}s`; } if (s.kind === "cron") return `cron: ${s.expr || ""}`; if (s.kind === "at") return `at: ${_formatMs(s.atMs)}`; return s.kind; };
  const _sessionLabel = (sk: string | null) => {
    if (!sk || sk === "default") return "default";
    const s = sessions.find((x) => x.sessionId === sk);
    return s ? (s.title.length > 24 ? s.title.slice(0, 24) + "…" : s.title) : sk.slice(0, 12);
  };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;

  return (
    <Section title="Cron Jobs" desc="定时任务管理。可为不同 session 配置独立的定时任务。">
      <div className="rounded-xl border border-border/50 p-4 mb-5 space-y-3">
        <div className="flex gap-3">
          <div className="flex-1"><label className="text-[11px] font-medium text-muted-foreground">名称（可选）</label><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="作业名称..." className="mt-1" /></div>
          <div className="w-44"><label className="text-[11px] font-medium text-muted-foreground">目标 Session</label>
            <select value={targetSessionId} onChange={(e) => setTargetSessionId(e.target.value)} className="mt-1 w-full rounded-lg border border-border/80 bg-transparent px-2 py-2 text-xs outline-none focus:border-primary/50">
              {sessions.map((s) => <option key={s.sessionId} value={s.sessionId}>{s.title || s.sessionId}</option>)}
              {sessions.length === 0 && <option value="default">default</option>}
            </select>
          </div>
        </div>
        <div><label className="text-[11px] font-medium text-muted-foreground">消息内容</label><Input value={message} onChange={(e) => setMessage(e.target.value)} placeholder="触发时发送给 LLM 的消息..." className="mt-1" /></div>
        <div><label className="text-[11px] font-medium text-muted-foreground">调度类型</label>
          <select value={scheduleKind} onChange={(e) => setScheduleKind(e.target.value as any)} className="mt-1 w-full rounded-lg border border-border/80 bg-transparent px-3 py-2 text-sm outline-none focus:border-primary/50">
            <option value="every">固定间隔</option><option value="cron">Cron 表达式</option><option value="at">一次性（at）</option>
          </select>
        </div>
        {scheduleKind === "every" && <div><label className="text-[11px] font-medium text-muted-foreground">间隔（秒）</label><Input value={everySeconds} onChange={(e) => setEverySeconds(e.target.value)} placeholder="3600（1 小时）" className="mt-1" /></div>}
        {scheduleKind === "cron" && (<div className="flex gap-3"><div className="flex-1"><label className="text-[11px] font-medium text-muted-foreground">表达式</label><Input value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} placeholder="0 9 * * 1-5" className="mt-1" /></div><div className="w-36"><label className="text-[11px] font-medium text-muted-foreground">时区</label><Input value={tz} onChange={(e) => setTz(e.target.value)} placeholder="自动（系统时区）" className="mt-1" /></div></div>)}
        {scheduleKind === "at" && <div className="flex gap-3"><div className="flex-1"><label className="text-[11px] font-medium text-muted-foreground">时间（ISO）</label><Input value={at} onChange={(e) => setAt(e.target.value)} placeholder="2026-07-11T15:30:00" className="mt-1" /></div><div className="w-36"><label className="text-[11px] font-medium text-muted-foreground">时区</label><Input value={tz} onChange={(e) => setTz(e.target.value)} placeholder="自动（系统时区）" className="mt-1" /></div></div>}
        <div className="flex items-center gap-3 pt-1"><Button size="sm" onClick={handleCreate}>创建</Button>{status && <span className="text-xs text-muted-foreground">{status}</span>}</div>
      </div>
      {jobs.length === 0 && <p className="text-sm text-muted-foreground/60">暂无定时作业</p>}
      {jobs.map((j) => (
        <div key={j.id} className="py-3 border-b border-border/40">
          <div className="flex items-center gap-2">
            <span className={cn("text-sm font-medium", !j.enabled && "text-muted-foreground line-through")}>{j.name || j.id}</span>
            {j.payload.kind === "system_event" && <span className="rounded-md bg-blue-100 dark:bg-blue-900/50 px-1.5 py-0.5 text-[10px] text-blue-600 dark:text-blue-400 font-medium">系统</span>}
            {!j.enabled && <span className="rounded-md bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground font-medium">已禁用</span>}
          </div>
          <p className="text-[10px] text-muted-foreground/60 mt-0.5">
            {j.id}　{_label(j)}
            {j.payload.sessionKey && <span>　session: {_sessionLabel(j.payload.sessionKey)}</span>}
            {j.state.lastRunAtMs && <span>　上次: {_formatMs(j.state.lastRunAtMs)}</span>}
            {j.state.lastStatus && <span>　{j.state.lastStatus}</span>}
            {j.state.lastError && <span className="text-red-500">　{j.state.lastError.slice(0, 40)}</span>}
          </p>
          {j.payload.kind !== "system_event" && (
            <div className="mt-1.5 flex gap-1.5">
              <Button variant="ghost" size="sm" className="h-6 text-[10px]" onClick={async () => { try { if (j.enabled) await disableCronJob(j.id); else await enableCronJob(j.id); refresh(); } catch {} }}>{j.enabled ? "禁用" : "启用"}</Button>
              <Button variant="ghost" size="sm" className="h-6 text-[10px] text-destructive hover:bg-destructive/10" onClick={async () => { if (!confirm("删除？")) return; try { await deleteCronJob(j.id); refresh(); } catch {} }}>删除</Button>
            </div>
          )}
        </div>
      ))}
    </Section>
  );
}

/* --- Skills --- */
function SkillsSection() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchSkills();
      if (data.ok) setSkills(data.skills || []);
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const handleUpload = async () => {
    if (!file) {
      setError("请选择 .zip、.tar、.tar.gz 或 .tgz 技能压缩包");
      return;
    }
    setBusy(true);
    setStatus("");
    setError("");
    try {
      const result = await uploadSkillPackage(file, replace);
      setStatus(`${result.message}，包含 ${result.skill.fileCount} 个文件`);
      setFile(null);
      const input = document.getElementById("skill-package-input") as HTMLInputElement | null;
      if (input) input.value = "";
      await refresh();
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`确定彻底删除 Skill “${name}” 吗？此操作会删除相关文件和使用记录，无法从 .archive 恢复。`)) return;
    setBusy(true);
    setStatus("");
    setError("");
    try {
      const result = await removeSkill(name);
      setStatus(result.message || `Skill ${name} 已删除`);
      await refresh();
    } catch (err) {
      setError(loadErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;

  return (
    <Section title="Skills" desc={`安装、校验和删除可复用技能（${skills.length} 个）。`}>
      <div className="mb-5 rounded-xl border border-border/60 bg-card/40 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-sm font-semibold">添加 Skill</p>
            <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
              上传包含 SKILL.md 的压缩包。服务端会校验格式、完整性、路径安全、文件类型和 frontmatter。
            </p>
          </div>
          <PackagePlus className="h-5 w-5 text-muted-foreground" />
        </div>
        <div className="mt-4 space-y-3">
          <input
            id="skill-package-input"
            type="file"
            accept=".zip,.tar,.gz,.tgz"
            className="sr-only"
            disabled={busy}
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          <div className="flex min-h-10 items-center gap-2 rounded-lg border border-border/80 bg-background/40 px-2 py-2">
            <Button asChild variant="outline" size="sm" className="shrink-0 gap-1.5">
              <label htmlFor="skill-package-input" className={cn(busy && "pointer-events-none opacity-50")}>
                <Upload className="h-3.5 w-3.5" />
                选择文件
              </label>
            </Button>
            <span className={cn(
              "min-w-0 flex-1 truncate text-xs",
              file ? "text-foreground" : "text-muted-foreground"
            )}>
              {file ? file.name : "未选择文件，支持 .zip / .tar / .tar.gz / .tgz"}
            </span>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <label className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={replace}
                disabled={busy}
                onChange={(e) => setReplace(e.target.checked)}
                className="h-3.5 w-3.5 shrink-0 rounded border-border"
              />
              <span className="leading-relaxed">同名 Skill 已存在时，先安全删除旧版本再安装</span>
            </label>
            <Button size="sm" onClick={handleUpload} disabled={busy || !file} className="shrink-0">
              {busy ? "处理中..." : "上传并安装"}
            </Button>
          </div>
          {status && <p className="text-xs text-emerald-600 dark:text-emerald-400">{status}</p>}
          {error && <p className="text-xs text-destructive" role="alert">{error}</p>}
        </div>
      </div>

      {skills.length === 0 && <p className="text-sm text-muted-foreground/60">暂无可用技能</p>}
      {skills.map((s) => (
        <div key={s.name} className="mb-2.5 rounded-xl border border-border/60 bg-card/45 p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm font-semibold">{s.name}</p>
              <p className="mt-0.5 text-[13px] leading-relaxed text-muted-foreground/70">{s.description}</p>
              {(s.hasAssets || s.hasReferences) && (
                <p className="mt-1.5 text-[10px] text-muted-foreground/60">
                  {[s.hasAssets && "包含资源", s.hasReferences && "包含参考资料"].filter(Boolean).join("　")}
                </p>
              )}
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              disabled={busy}
              className="shrink-0 text-destructive hover:bg-destructive/10"
              title={`删除 ${s.name}`}
              onClick={() => void handleDelete(s.name)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      ))}
    </Section>
  );
}
