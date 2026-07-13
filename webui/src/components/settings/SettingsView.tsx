import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Moon, Sun, FileText, Brain, Database, Clock, Wrench, PawPrint } from "lucide-react";
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
} from "@/lib/api";
import type { SettingsSection, MemoryEntry, CronJobInfo, SkillInfo, SessionSummary } from "@/lib/types";
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
  const [tz, setTz] = useState("Asia/Shanghai"); const [at, setAt] = useState("");
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
      else if (scheduleKind === "cron") { if (!cronExpr.trim()) { setStatus("请填写 cron 表达式"); return; } payload.cronExpr = cronExpr.trim(); payload.tz = tz.trim() || "UTC"; }
      else { if (!at.trim()) { setStatus("请填写 ISO 时间"); return; } payload.at = at.trim(); }
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
        {scheduleKind === "cron" && (<div className="flex gap-3"><div className="flex-1"><label className="text-[11px] font-medium text-muted-foreground">表达式</label><Input value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} placeholder="0 9 * * 1-5" className="mt-1" /></div><div className="w-36"><label className="text-[11px] font-medium text-muted-foreground">时区</label><Input value={tz} onChange={(e) => setTz(e.target.value)} className="mt-1" /></div></div>)}
        {scheduleKind === "at" && <div><label className="text-[11px] font-medium text-muted-foreground">时间（ISO）</label><Input value={at} onChange={(e) => setAt(e.target.value)} placeholder="2026-07-11T15:30:00" className="mt-1" /></div>}
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

  useEffect(() => {
    let cancelled = false;
    fetchSkills().then((d) => { if (!cancelled && d.ok) setSkills(d.skills || []); }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  if (loading) return <p className="text-sm text-muted-foreground/60">加载中...</p>;

  return (
    <Section title="Skills" desc={`可用技能列表（${skills.length} 个）。`}>
      {skills.map((s) => (
        <div key={s.name} className="mb-2.5 rounded-xl border border-border/60 bg-card/45 p-4">
          <p className="text-sm font-semibold">{s.name}</p>
          <p className="text-[13px] text-muted-foreground/70 mt-0.5 leading-relaxed">{s.description}</p>
          {(s.hasAssets || s.hasReferences) && <p className="mt-1.5 text-[10px] text-muted-foreground/60">{[s.hasAssets && "包含资源", s.hasReferences && "包含参考资料"].filter(Boolean).join("　")}</p>}
        </div>
      ))}
    </Section>
  );
}
