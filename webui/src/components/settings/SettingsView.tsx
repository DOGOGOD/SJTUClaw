import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, Moon, Sun, FileText, Brain, Database, Clock, Wrench } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/input";
import { cn, formatTime } from "@/lib/utils";
import {
  fetchSystemPrompt, saveSystemPrompt,
  fetchSoul, saveSoul,
  fetchMemories, addMemory, deleteMemory,
  fetchTasks, createTask, cancelTask, deleteTask,
  fetchSkills,
  fetchWorkspace, setWorkspace, unsetWorkspace,
} from "@/lib/api";
import type {
  SettingsSection,
  MemoryEntry,
  TaskInfo,
  SkillInfo,
  WorkspaceInfo,
} from "@/lib/types";

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
  { key: "tasks", label: "Tasks", Icon: Clock },
  { key: "skills", label: "Skills", Icon: Wrench },
];

export function SettingsView({
  theme,
  activeSection,
  onToggleTheme,
  onBackToChat,
  onSectionChange,
  activeSessionId,
}: SettingsViewProps) {
  return (
    <div className="flex h-full bg-background">
      {/* Sidebar nav */}
      <nav className="w-48 shrink-0 border-r border-border bg-sidebar p-3 flex flex-col gap-0.5">
        <div className="mb-3 flex items-center gap-2">
          <Button variant="ghost" size="icon-sm" onClick={onBackToChat} title="返回对话">
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <span className="text-sm font-semibold">设置</span>
        </div>
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            onClick={() => onSectionChange(s.key)}
            className={cn(
              "flex items-center gap-2 rounded-md px-2 py-1.5 text-xs text-left transition-colors hover:bg-sidebar-accent",
              activeSection === s.key && "bg-sidebar-accent font-medium"
            )}
          >
            <s.Icon className="h-3.5 w-3.5 text-muted-foreground" />
            <span>{s.label}</span>
          </button>
        ))}
        <div className="mt-auto">
          <button
            onClick={onToggleTheme}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors hover:bg-sidebar-accent text-muted-foreground"
          >
            {theme === "dark" ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
            <span>{theme === "dark" ? "浅色模式" : "深色模式"}</span>
          </button>
        </div>
      </nav>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6">
        <div className="max-w-2xl">
          {activeSection === "prompt" && <SystemPromptSection />}
          {activeSection === "soul" && <SoulSection />}
          {activeSection === "memory" && <MemorySection />}
          {activeSection === "tasks" && <TasksSection />}
          {activeSection === "skills" && <SkillsSection />}
          {activeSection === "workspace" && <WorkspaceSection sessionId={activeSessionId} />}
        </div>
      </div>
    </div>
  );
}

/* --- System Prompt --- */
function SystemPromptSection() {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchSystemPrompt().then((d) => {
      if (!cancelled) { setContent(d.content || ""); setLoading(false); }
    }).catch(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await saveSystemPrompt(content);
      setStatus("已保存");
      setTimeout(() => setStatus(""), 2000);
    } catch {
      setStatus("保存失败");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">System Prompt</h2>
      <p className="text-xs text-muted-foreground mb-4">定义 Agent 的核心行为规则。修改后无需重启。</p>
      <Textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        className="min-h-[300px] font-mono text-sm"
      />
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={handleSave} disabled={saving}>{saving ? "保存中..." : "保存"}</Button>
        {status && <span className="text-xs text-muted-foreground">{status}</span>}
      </div>
    </div>
  );
}

/* --- Soul --- */
function SoulSection() {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchSoul().then((d) => {
      if (!cancelled) { setContent(d.content || ""); setLoading(false); }
    }).catch(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await saveSoul(content);
      setStatus("已保存");
      setTimeout(() => setStatus(""), 2000);
    } catch {
      setStatus("保存失败");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">Soul</h2>
      <p className="text-xs text-muted-foreground mb-4">定义 Agent 的人格和交互风格。</p>
      <Textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        className="min-h-[300px] font-mono text-sm"
      />
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={handleSave} disabled={saving}>{saving ? "保存中..." : "保存"}</Button>
        {status && <span className="text-xs text-muted-foreground">{status}</span>}
      </div>
    </div>
  );
}

/* --- Memory --- */
function MemorySection() {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const d = await fetchMemories();
      setMemories(d.memories || []);
    } catch {} finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleAdd = async () => {
    if (!input.trim()) return;
    try {
      await addMemory(input.trim());
      setInput("");
      refresh();
    } catch {}
  };

  const handleDelete = async (id: string) => {
    if (!confirm("确定删除？")) return;
    try {
      await deleteMemory(id);
      refresh();
    } catch {}
  };

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">Memory</h2>
      <p className="text-xs text-muted-foreground mb-4">长期记忆，跨 session 持久化。</p>
      <div className="flex gap-2 mb-4">
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入新的记忆内容..."
          onKeyDown={(e) => e.key === "Enter" && handleAdd()}
        />
        <Button onClick={handleAdd} className="shrink-0">添加</Button>
      </div>
      {memories.length === 0 && <p className="text-sm text-muted-foreground">暂无长期记忆</p>}
      {memories.map((m) => (
        <div key={m.id} className="flex items-start gap-3 border-b border-border py-2">
          <div className="flex-1 min-w-0">
            <p className="text-sm">{m.content}</p>
            <p className="text-[10px] text-muted-foreground mt-0.5">
              {m.id} · {m.category || "general"} · {formatTime(m.createdAt)}
            </p>
          </div>
          <Button variant="ghost" size="icon-sm" className="text-destructive shrink-0" onClick={() => handleDelete(m.id)}>
            删除
          </Button>
        </div>
      ))}
    </div>
  );
}

/* --- Tasks --- */
function TasksSection() {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [content, setContent] = useState("");
  const [triggerType, setTriggerType] = useState("once");
  const [triggerRule, setTriggerRule] = useState("");
  const [sessionId, setSessionId] = useState("default");
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("");

  const refresh = useCallback(async () => {
    try {
      const d = await fetchTasks();
      setTasks(d.tasks || []);
    } catch {} finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleCreate = async () => {
    if (!content.trim() || !triggerRule.trim()) {
      setStatus("请填写完整");
      return;
    }
    try {
      const d = await createTask({ content: content.trim(), triggerType, triggerRule: triggerRule.trim(), sessionId });
      if (d.ok) {
        setStatus("创建成功");
        setContent("");
        setTriggerRule("");
        refresh();
      } else {
        setStatus("创建失败");
      }
    } catch {
      setStatus("创建失败");
    }
  };

  const handleCancel = async (id: string) => {
    if (!confirm("取消任务？")) return;
    try { await cancelTask(id); refresh(); } catch {}
  };

  const handleDelete = async (id: string) => {
    if (!confirm("删除任务？")) return;
    try { await deleteTask(id); refresh(); } catch {}
  };

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">Tasks</h2>
      <p className="text-xs text-muted-foreground mb-4">定时任务管理。</p>
      <div className="rounded-lg border border-border p-4 mb-4 space-y-3">
        <div>
          <label className="text-xs font-medium">任务内容</label>
          <Input value={content} onChange={(e) => setContent(e.target.value)} placeholder="发给 LLM 的消息..." />
        </div>
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="text-xs font-medium">类型</label>
            <select
              value={triggerType}
              onChange={(e) => setTriggerType(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="once">一次性</option>
              <option value="interval">固定间隔</option>
              <option value="daily">每天定时</option>
            </select>
          </div>
          <div className="flex-1">
            <label className="text-xs font-medium">规则</label>
            <Input
              value={triggerRule}
              onChange={(e) => setTriggerRule(e.target.value)}
              placeholder={triggerType === "once" ? "2026-07-10T15:30:00" : triggerType === "interval" ? "300 (秒)" : "09:00"}
            />
          </div>
        </div>
        <div>
          <label className="text-xs font-medium">Session</label>
          <Input value={sessionId} onChange={(e) => setSessionId(e.target.value)} />
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={handleCreate}>创建</Button>
          {status && <span className="text-xs text-muted-foreground">{status}</span>}
        </div>
      </div>
      {tasks.map((t) => (
        <div key={t.id} className="border-b border-border py-2">
          <p className="text-sm font-medium">{t.content}</p>
          <p className="text-[10px] text-muted-foreground">
            {t.id} · {t.triggerType} · {t.triggerRule} · {t.status}
            {t.nextRunAt && ` · 下次: ${formatTime(t.nextRunAt)}`}
          </p>
          <div className="mt-1 flex gap-2">
            {(t.status === "waiting" || t.status === "running") && (
              <Button variant="ghost" size="sm" className="text-destructive h-6 text-[10px]" onClick={() => handleCancel(t.id)}>取消</Button>
            )}
            {(t.status === "completed" || t.status === "cancelled" || t.status === "failed") && (
              <Button variant="ghost" size="sm" className="text-destructive h-6 text-[10px]" onClick={() => handleDelete(t.id)}>删除</Button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/* --- Skills --- */
function SkillsSection() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchSkills()
      .then((d) => { if (!cancelled && d.ok) setSkills(d.skills || []); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">Skills ({skills.length})</h2>
      <p className="text-xs text-muted-foreground mb-4">可用技能列表。</p>
      {skills.map((s) => (
        <div key={s.name} className="rounded-lg border border-border p-3 mb-2">
          <p className="text-sm font-medium">{s.name}</p>
          <p className="text-xs text-muted-foreground">{s.description}</p>
          <p className="text-[10px] text-muted-foreground mt-1">
            {s.hasAssets && "有资源 "}{s.hasReferences && "有参考 "}
          </p>
        </div>
      ))}
    </div>
  );
}

/* --- Workspace (used inline, not in settings nav) --- */
function WorkspaceSection({ sessionId }: { sessionId?: string | null }) {
  const [ws, setWs] = useState<WorkspaceInfo | null>(null);
  const [path, setPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("");
  const sid = sessionId || "default";

  const refresh = useCallback(async () => {
    try {
      const d = await fetchWorkspace(sid);
      setWs(d);
      setPath(d.workspace || "");
    } catch {} finally {
      setLoading(false);
    }
  }, [sid]);

  useEffect(() => { refresh(); }, [refresh]);

  const handleSet = async () => {
    if (!path.trim()) { setStatus("请输入路径"); return; }
    try {
      const d = await setWorkspace(sid, path.trim());
      if (d.ok) { setStatus("已设置"); refresh(); }
    } catch (e: any) {
      setStatus(e.message || "失败");
    }
  };

  const handleUnset = async () => {
    if (!confirm("确定取消？")) return;
    try { await unsetWorkspace(sid); setStatus("已取消"); refresh(); } catch { setStatus("失败"); }
  };

  if (loading) return <p className="text-sm text-muted-foreground">加载中...</p>;

  return (
    <div>
      <h2 className="text-lg font-semibold mb-1">Workspace</h2>
      <p className="text-xs text-muted-foreground mb-4">设置工作区路径。Session: {sid}</p>
      <div className="flex gap-2 mb-2">
        <Input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="C:\Users\...\project"
          onKeyDown={(e) => e.key === "Enter" && handleSet()}
        />
        <Button onClick={handleSet} className="shrink-0">设置</Button>
      </div>
      {ws?.workspace && (
        <Button variant="destructive" size="sm" onClick={handleUnset}>取消设置</Button>
      )}
      {status && <p className="mt-2 text-xs text-muted-foreground">{status}</p>}
      <p className="mt-3 text-[11px] text-muted-foreground">
        · 相对路径按 workspace 解析<br />
        · 不允许 ../ 或绝对路径逃逸
      </p>
    </div>
  );
}
