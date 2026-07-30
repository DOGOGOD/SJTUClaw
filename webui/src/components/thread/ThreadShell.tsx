import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bot, Box, History, PanelLeft, Moon, Sun, ShieldCheck, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ThreadViewport } from "./ThreadViewport";
import { ThreadComposer } from "./ThreadComposer";
import { useDragScroll } from "@/hooks/useDragScroll";
import type { AgentBackend, ChatMessage } from "@/lib/types";

interface ThreadShellProps {
  sessionId: string | null;
  title: string;
  messages: ChatMessage[];
  loading: boolean;
  sending: boolean;
  autoMode?: boolean;
  sandboxMode?: boolean;
  unlimitedMode?: boolean;
  piMode?: boolean;
  agentBackend?: AgentBackend;
  rollbackEnabled?: boolean;
  rollingBack?: boolean;
  workspaceRefreshToken?: number;
  onRollback?: (checkpointId: string) => Promise<void>;
  onSend: (message: string, attachments?: File[]) => Promise<void>;
  onStop?: () => Promise<void>;
  onToggleSidebar?: () => void;
  onNewChat: () => void;
  onCreateWorkspaceSession?: (path: string) => Promise<void>;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  hideSidebarToggle?: boolean;
}

export function SessionModeBadges({
  autoMode,
  sandboxMode = false,
  unlimitedMode,
  rollbackEnabled = false,
  piMode,
  agentBackend,
}: {
  autoMode: boolean;
  sandboxMode?: boolean;
  unlimitedMode: boolean;
  rollbackEnabled?: boolean;
  piMode: boolean;
  agentBackend?: AgentBackend;
}) {
  const backend = agentBackend ?? (piMode ? "pi" : "sjtuclaw");
  return (
    <>
      {autoMode && (
        <span className="flex shrink-0 items-center gap-1 rounded-lg border border-primary/20 bg-primary/10 px-2 py-1 text-[10px] font-semibold text-primary">
          <Zap className="h-3 w-3" /> Auto
        </span>
      )}
      {sandboxMode && (
        <span
          data-testid="sandbox-mode-badge"
          className="flex shrink-0 items-center gap-1 rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-2 py-1 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300"
          title="当前会话的原生文件和 Shell 工具运行在隔离 microVM 中"
        >
          <Box className="h-3 w-3" /> Sandbox
        </span>
      )}
      {unlimitedMode && (
        <span className="flex shrink-0 items-center gap-1 rounded-lg border border-destructive/25 bg-destructive/10 px-2 py-1 text-[10px] font-semibold text-destructive">
          <ShieldCheck className="h-3 w-3" /> Unlimited
        </span>
      )}
      {rollbackEnabled && (
        <span
          data-testid="rollback-mode-badge"
          className="flex shrink-0 items-center gap-1 rounded-lg border border-sky-500/25 bg-sky-500/10 px-2 py-1 text-[10px] font-semibold text-sky-700 dark:text-sky-300"
          title="当前会话已开启 Workspace 回退"
        >
          <History className="h-3 w-3" /> Rollback
        </span>
      )}
      {backend === "pi" && (
        <span
          data-testid="pi-mode-badge"
          className="flex shrink-0 items-center gap-1 rounded-lg border border-violet-500/25 bg-violet-500/10 px-2 py-1 text-[10px] font-semibold text-violet-600 dark:text-violet-300"
          title="当前会话使用 Pi Agent 后端"
        >
          <Bot className="h-3 w-3" /> Pi
        </span>
      )}
      {backend === "claude" && (
        <span
          data-testid="claude-mode-badge"
          className="flex shrink-0 items-center gap-1 rounded-lg border border-orange-500/25 bg-orange-500/10 px-2 py-1 text-[10px] font-semibold text-orange-700 dark:text-orange-300"
          title="当前会话使用 Claude Code 后端"
        >
          <Bot className="h-3 w-3" /> Claude Code
        </span>
      )}
    </>
  );
}

export function ThreadShell({
  sessionId,
  title,
  messages,
  loading,
  sending,
  onSend,
  onStop,
  onToggleSidebar,
  onCreateWorkspaceSession,
  onToggleTheme,
  autoMode = false,
  sandboxMode = false,
  unlimitedMode = false,
  piMode = false,
  agentBackend,
  rollbackEnabled = false,
  rollingBack = false,
  workspaceRefreshToken = 0,
  onRollback,
  theme,
  hideSidebarToggle = false,
}: ThreadShellProps) {
  const [autoScroll, setAutoScroll] = useState(() => Boolean(sessionId));
  const messageHistory = useMemo(
    () => messages.filter((message) => message.role === "user").map((message) => message.content),
    [messages]
  );
  const {
    ref: viewportRef,
    dragScrollProps: messageDragProps,
  } = useDragScroll<HTMLDivElement>({
    axis: "y",
    onDrag: () => setAutoScroll(false),
  });

  // Every session owns an independent scroll position. Entering or switching
  // a session should start at its latest message, even if the user had scrolled
  // up while viewing the previous session.
  useEffect(() => {
    setAutoScroll(Boolean(sessionId));
  }, [sessionId]);

  // When the user sends a message (sending transitions to true), enable
  // auto-scroll so new replies and tool results scroll into view.
  const prevSending = useRef(sending);
  useEffect(() => {
    if (sending && !prevSending.current) {
      setAutoScroll(true);
    }
    prevSending.current = sending;
  }, [sending]);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const threshold = 80;
    setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < threshold);
  }, []);

  useEffect(() => {
    if (autoScroll && viewportRef.current) {
      requestAnimationFrame(() => {
        viewportRef.current?.scrollTo({
          top: viewportRef.current.scrollHeight,
          behavior: "instant" as ScrollBehavior,
        });
      });
    }
  }, [messages, autoScroll]);

  // Message blocks can grow after their first render (lazy syntax highlighting,
  // images, expanded tool output). Keep following those layout changes only
  // while auto-scroll is active; handleScroll disables it as soon as the user
  // moves away from the bottom.
  useEffect(() => {
    const viewport = viewportRef.current;
    const content = viewport?.firstElementChild;
    if (!autoScroll || !viewport || !content || typeof ResizeObserver === "undefined") return;

    let frame = 0;
    const scrollToLatest = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        viewport.scrollTo({
          top: viewport.scrollHeight,
          behavior: "instant" as ScrollBehavior,
        });
      });
    };
    const observer = new ResizeObserver(scrollToLatest);
    observer.observe(content);
    scrollToLatest();

    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [autoScroll, loading, messages, sessionId]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-transparent">
      <header className="host-drag-region flex h-14 shrink-0 items-center gap-3 border-b border-border/60 bg-background/80 px-4 backdrop-blur-xl md:px-5">
        {!hideSidebarToggle && onToggleSidebar && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onToggleSidebar}
            title="切换侧栏"
            className="host-no-drag"
          >
            <PanelLeft className="h-4 w-4" />
          </Button>
        )}
        <div className="flex-1 min-w-0 flex items-center gap-2">
          <h1 className="truncate text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
            {title || "SJTUClaw"}
          </h1>
          <SessionModeBadges
            autoMode={autoMode}
            sandboxMode={sandboxMode}
            unlimitedMode={unlimitedMode}
            rollbackEnabled={rollbackEnabled}
            piMode={piMode}
            agentBackend={agentBackend}
          />
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onToggleTheme}
          title={theme === "dark" ? "浅色模式" : "深色模式"}
          className="host-no-drag"
        >
          {theme === "dark" ? (
            <Sun className="h-4 w-4" />
          ) : (
            <Moon className="h-4 w-4" />
          )}
        </Button>
      </header>

      {!sessionId ? (
        <div className="flex flex-1 min-h-0 overflow-y-auto px-4 py-8 md:px-8">
          <div className="mx-auto flex min-h-full w-full max-w-[760px] flex-col justify-center pb-[8vh]">
            <ThreadViewport messages={messages} loading={loading} sessionId={sessionId} rollbackEnabled={rollbackEnabled} rollingBack={rollingBack} onRollback={onRollback} />
            <div className="mt-8">
              <ThreadComposer
                onSend={onSend}
                sessionId={sessionId}
                messageHistory={messageHistory}
                sending={sending}
                workspaceRefreshToken={workspaceRefreshToken}
                onCreateWorkspaceSession={onCreateWorkspaceSession}
                home
              />
            </div>
            <p className="mt-3 text-center text-[10px] text-muted-foreground/55 select-none">
              Claw 可能会犯错，请核对重要信息
            </p>
          </div>
        </div>
      ) : (
        <>
          <div
            ref={viewportRef}
            {...messageDragProps}
            onScroll={handleScroll}
            data-testid="thread-scroll-viewport"
            className="host-no-drag drag-scroll scroll-container min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-y-contain"
          >
            <ThreadViewport messages={messages} loading={loading} sessionId={sessionId} rollbackEnabled={rollbackEnabled} rollingBack={rollingBack} onRollback={onRollback} />
          </div>
          <div className="host-no-drag shrink-0 bg-gradient-to-t from-background via-background to-background/80 px-3 pb-3 pt-2 md:px-6 md:pb-5">
            <div className="mx-auto max-w-[880px]">
              <ThreadComposer onSend={onSend} onStop={onStop} sessionId={sessionId} messageHistory={messageHistory} sending={sending || rollingBack} workspaceRefreshToken={workspaceRefreshToken} />
            </div>
            <p className="mt-2 text-center text-[10px] text-muted-foreground/50 select-none">
              Enter 发送　Shift+Enter 换行　输入 / 查看命令
            </p>
          </div>
        </>
      )}
    </div>
  );
}
