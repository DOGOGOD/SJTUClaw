import { memo, useCallback, useState } from "react";
import { Plus, Search, Trash2, Pencil, Settings } from "lucide-react";
import { BrandAvatar } from "@/components/BrandAvatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn, formatTime, deriveTitle } from "@/lib/utils";
import type { SessionSummary, ShellView, SettingsSection } from "@/lib/types";

interface SidebarProps {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  loading: boolean;
  onNewChat: () => void;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  onRename: (sessionId: string) => void;
  onOpenSettings: (section?: SettingsSection) => void;
  onToggleSidebar?: () => void;
  collapsed?: boolean;
  activeUtility?: ShellView | null;
  interactionLocked?: boolean;
}

export const Sidebar = memo(function Sidebar({
  sessions,
  activeSessionId,
  loading,
  onNewChat,
  onSelect,
  onDelete,
  onRename,
  onOpenSettings,
  collapsed = false,
  activeUtility,
  interactionLocked = false,
}: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const filtered = searchQuery.trim()
    ? sessions.filter(
        (s) =>
          s.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
          s.sessionId.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : sessions;

  const handleDelete = useCallback(
    (e: React.MouseEvent, sessionId: string) => {
      e.stopPropagation();
      if (confirm("确定删除此对话？")) onDelete(sessionId);
    },
    [onDelete]
  );

  const handleRename = useCallback(
    (e: React.MouseEvent, sessionId: string) => {
      e.stopPropagation();
      onRename(sessionId);
    },
    [onRename]
  );

  if (collapsed) return null;

  return (
    <div className="flex h-full min-h-0 flex-col bg-sidebar text-sidebar-foreground">
      <div className="host-drag-region sticky top-0 z-10 shrink-0 border-b border-border/60 bg-sidebar">
        {/* Header */}
        <div className="flex h-14 items-center gap-2.5 px-4">
          <BrandAvatar className="h-8 w-8" fullCharacter />
          <span className="flex-1 font-semibold text-sm tracking-[-0.015em] select-none">
            SJTUClaw
          </span>
          <div className="host-no-drag flex items-center gap-0.5">
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => onOpenSettings("prompt")}
              title="设置"
              className={activeUtility === "settings" ? "text-foreground bg-sidebar-accent" : ""}
            >
              <Settings className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        <div className="host-no-drag px-3 pb-3 pt-1">
          <Button
            onClick={onNewChat}
            disabled={interactionLocked}
            className="mb-3 h-9 w-full justify-start gap-2 bg-sidebar-accent text-sidebar-accent-foreground shadow-none hover:bg-sidebar-accent/80"
          >
            <Plus className="h-4 w-4" strokeWidth={1.8} />
            新对话
          </Button>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/55" />
            <Input
              placeholder="搜索对话"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-9 pl-9 text-[12px] rounded-xl bg-background/55 border-border/60 focus:bg-background"
            />
          </div>
        </div>
      </div>

      {/* Session list */}
      <div
        className="host-no-drag scroll-container min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-y-contain px-2 pb-3"
      >
        {!loading && filtered.length > 0 && (
          <div className="px-3 pb-2 pt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground/55">近期对话</div>
        )}
        {loading && (
          <p className="px-3 py-10 text-center text-[12px] text-muted-foreground/60">
            加载中…
          </p>
        )}
        {!loading && filtered.length === 0 && (
          <p className="px-3 py-10 text-center text-[12px] text-muted-foreground/60">
            {searchQuery ? "无匹配结果" : "暂无对话"}
          </p>
        )}
        {filtered.map((s, i) => {
          const isActive = activeSessionId === s.sessionId;
          return (
            <div
              key={s.sessionId}
              role="button"
              tabIndex={interactionLocked ? -1 : 0}
              aria-disabled={interactionLocked}
              onClick={() => { if (!interactionLocked) onSelect(s.sessionId); }}
              onKeyDown={(event) => {
                if (interactionLocked || (event.key !== "Enter" && event.key !== " ")) return;
                event.preventDefault();
                onSelect(s.sessionId);
              }}
              onMouseEnter={() => setHoveredId(s.sessionId)}
              onMouseLeave={() => setHoveredId(null)}
              style={{ animationDelay: `${i * 30}ms` }}
              className={cn(
                "group relative w-full cursor-pointer rounded-xl px-3 py-2.5 text-left transition-colors duration-150 animate-enter-up focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40",
                interactionLocked && "cursor-not-allowed opacity-55",
                isActive
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/60"
              )}
            >
              <div className="truncate pr-12 text-[13px] font-medium leading-snug tracking-[-0.01em]">
                {s.title || deriveTitle(s.sessionId)}
              </div>
              <div className="mt-1 flex items-center justify-between pr-1 text-[10px] text-muted-foreground/60">
                <span>{s.messageCount} 条消息</span>
                <span>{formatTime(s.updatedAt)}</span>
              </div>
              {hoveredId === s.sessionId && (
                <div className="absolute right-1.5 top-1.5 flex items-center gap-0.5 animate-enter-scale">
                  <button
                    data-drag-scroll-ignore
                    onClick={(e) => handleRename(e, s.sessionId)}
                    className="rounded-md p-1.5 hover:bg-sidebar-accent transition-colors duration-150"
                    title="重命名"
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    data-drag-scroll-ignore
                    onClick={(e) => handleDelete(e, s.sessionId)}
                    className="rounded-md p-1.5 hover:bg-destructive/10 hover:text-destructive transition-colors duration-150"
                    title="删除"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
});
