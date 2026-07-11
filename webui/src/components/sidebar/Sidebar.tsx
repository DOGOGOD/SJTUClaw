import { useCallback, useState } from "react";
import { Plus, Search, Trash2, Pencil, Settings, PanelLeftClose, PanelLeft } from "lucide-react";
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
  onToggleCollapse?: () => void;
  activeUtility?: ShellView | null;
}

export function Sidebar({
  sessions,
  activeSessionId,
  loading,
  onNewChat,
  onSelect,
  onDelete,
  onRename,
  onOpenSettings,
  collapsed = false,
  onToggleCollapse,
  activeUtility,
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
      if (confirm("确定删除此对话？")) {
        onDelete(sessionId);
      }
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

  return (
    <div className={cn("flex h-full flex-col bg-sidebar text-sidebar-foreground", collapsed && "items-center")}>
      {/* Header */}
      <div className={cn(
        "flex items-center gap-2 px-3 py-3",
        collapsed && "flex-col justify-center px-1 gap-1"
      )}>
        {!collapsed ? (
          <>
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-base">🦞</div>
            <span className="flex-1 font-semibold text-sm truncate">SJTUClaw</span>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => onOpenSettings("prompt")}
              title="设置"
            >
              <Settings className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onNewChat}
              title="新对话"
            >
              <Plus className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onToggleCollapse}
              title="收起侧栏"
            >
              <PanelLeftClose className="h-4 w-4" />
            </Button>
          </>
        ) : (
          <>
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-lg">🦞</div>
            <Button
              variant="ghost"
              size="icon"
              onClick={onNewChat}
              title="新对话"
            >
              <Plus className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => onOpenSettings("prompt")}
              title="设置"
            >
              <Settings className="h-4 w-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={onToggleCollapse}
              title="展开侧栏"
            >
              <PanelLeft className="h-4 w-4" />
            </Button>
          </>
        )}
      </div>

      {/* Search — only when expanded */}
      {!collapsed && (
        <div className="px-3 pb-2">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="搜索对话…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-8 pl-7 text-xs"
            />
          </div>
        </div>
      )}

      {/* Collapsed search button */}
      {collapsed && (
        <div className="pb-1">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => onToggleCollapse?.()}
            title="搜索对话"
          >
            <Search className="h-4 w-4" />
          </Button>
        </div>
      )}

      {/* Session list — only when expanded */}
      {!collapsed && (
        <div className="flex-1 overflow-y-auto px-2">
          {loading && (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              加载中…
            </div>
          )}
          {!loading && filtered.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              {searchQuery ? "无匹配结果" : "暂无对话"}
            </div>
          )}
          {filtered.map((s) => (
            <button
              key={s.sessionId}
              onClick={() => onSelect(s.sessionId)}
              onMouseEnter={() => setHoveredId(s.sessionId)}
              onMouseLeave={() => setHoveredId(null)}
              className={cn(
                "group relative w-full rounded-md px-3 py-2 text-left transition-colors",
                activeSessionId === s.sessionId
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/50"
              )}
            >
              <div className="truncate text-xs font-medium">
                {s.title || deriveTitle(s.sessionId)}
              </div>
              <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted-foreground">
                <span>{s.messageCount} 条消息</span>
                <span>·</span>
                <span>{formatTime(s.updatedAt)}</span>
              </div>
              {hoveredId === s.sessionId && (
                <div className="absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-0.5">
                  <button
                    onClick={(e) => handleRename(e, s.sessionId)}
                    className="rounded p-1 hover:bg-sidebar-accent"
                    title="重命名"
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    onClick={(e) => handleDelete(e, s.sessionId)}
                    className="rounded p-1 hover:bg-destructive/20 hover:text-destructive"
                    title="删除"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              )}
            </button>
          ))}
        </div>
      )}

      {/* Collapsed session dots */}
      {collapsed && (
        <div className="flex-1 overflow-y-auto px-1 py-1 flex flex-col items-center gap-0.5">
          {filtered.slice(0, 8).map((s) => (
            <button
              key={s.sessionId}
              onClick={() => onSelect(s.sessionId)}
              className={cn(
                "h-7 w-7 rounded-md flex items-center justify-center text-[10px] font-medium transition-colors",
                activeSessionId === s.sessionId
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "hover:bg-sidebar-accent/50 text-muted-foreground"
              )}
              title={s.title || s.sessionId}
            >
              {(s.title || s.sessionId).slice(0, 2)}
            </button>
          ))}
          {filtered.length > 8 && (
            <span className="text-[9px] text-muted-foreground mt-1">
              +{filtered.length - 8}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
