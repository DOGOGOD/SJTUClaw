import { useCallback, useEffect, useRef, useState } from "react";
import { PanelLeft, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ThreadViewport } from "./ThreadViewport";
import { ThreadComposer } from "./ThreadComposer";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";

interface ThreadShellProps {
  sessionId: string | null;
  title: string;
  messages: ChatMessage[];
  loading: boolean;
  sending: boolean;
  autoMode?: boolean;
  onSend: (message: string) => Promise<void>;
  onAttach?: (file: File) => void;
  onToggleSidebar?: () => void;
  onNewChat: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  hideSidebarToggle?: boolean;
}

export function ThreadShell({
  sessionId,
  title,
  messages,
  loading,
  sending,
  onSend,
  onToggleSidebar,
  onToggleTheme,
  onAttach,
  autoMode = false,
  theme,
  hideSidebarToggle = false,
}: ThreadShellProps) {
  const [autoScroll, setAutoScroll] = useState(true);
  const viewportRef = useRef<HTMLDivElement>(null);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const threshold = 80;
    const isNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    setAutoScroll(isNearBottom);
  }, []);

  useEffect(() => {
    if (autoScroll && viewportRef.current) {
      viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
    }
  }, [messages, autoScroll]);

  return (
    <div className="flex h-full flex-col bg-background">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-border px-4 py-2.5">
        {!hideSidebarToggle && onToggleSidebar && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onToggleSidebar}
            title="切换侧栏"
          >
            <PanelLeft className="h-4 w-4" />
          </Button>
        )}
        <div className="flex-1 min-w-0 flex items-center gap-2">
          <h1 className="truncate text-sm font-medium">
            {title || "SJTUClaw"}
          </h1>
          {autoMode && (
            <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold text-amber-700 dark:bg-amber-900 dark:text-amber-300">
              AUTO
            </span>
          )}
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onToggleTheme}
          title="切换主题"
        >
          {theme === "dark" ? (
            <Sun className="h-4 w-4" />
          ) : (
            <Moon className="h-4 w-4" />
          )}
        </Button>
      </header>

      {/* Messages */}
      <div
        ref={viewportRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto"
      >
        <ThreadViewport
          messages={messages}
          loading={loading}
          sessionId={sessionId}
        />
      </div>

      {/* Composer */}
      <div className="border-t border-border bg-background px-4 py-3">
        <div className="mx-auto max-w-3xl">
          <ThreadComposer
            onSend={onSend}
            onAttach={onAttach}
            sessionId={sessionId}
            disabled={!sessionId || sending}
          />
        </div>
        <p className="mt-2 text-center text-[11px] text-muted-foreground">
          Enter 发送 · Shift+Enter 换行 · 以 / 开头执行命令
        </p>
      </div>
    </div>
  );
}
