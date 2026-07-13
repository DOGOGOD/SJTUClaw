import { useCallback, useEffect, useRef, useState } from "react";
import { PanelLeft, Moon, Sun, ShieldCheck, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ThreadViewport } from "./ThreadViewport";
import { ThreadComposer } from "./ThreadComposer";
import { useDragScroll } from "@/hooks/useDragScroll";
import type { ChatMessage } from "@/lib/types";

interface ThreadShellProps {
  sessionId: string | null;
  title: string;
  messages: ChatMessage[];
  loading: boolean;
  sending: boolean;
  autoMode?: boolean;
  unlimitedMode?: boolean;
  onSend: (message: string) => Promise<void>;
  onStop?: () => Promise<void>;
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
  onStop,
  onToggleSidebar,
  onToggleTheme,
  onAttach,
  autoMode = false,
  unlimitedMode = false,
  theme,
  hideSidebarToggle = false,
}: ThreadShellProps) {
  const [autoScroll, setAutoScroll] = useState(false);
  const {
    ref: viewportRef,
    dragScrollProps: messageDragProps,
  } = useDragScroll<HTMLDivElement>({
    axis: "y",
    onDrag: () => setAutoScroll(false),
  });

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
          {autoMode && (
            <span className="flex shrink-0 items-center gap-1 rounded-lg border border-primary/20 bg-primary/10 px-2 py-1 text-[10px] font-semibold text-primary">
              <Zap className="h-3 w-3" /> Auto
            </span>
          )}
          {unlimitedMode && (
            <span className="flex shrink-0 items-center gap-1 rounded-lg border border-destructive/25 bg-destructive/10 px-2 py-1 text-[10px] font-semibold text-destructive">
              <ShieldCheck className="h-3 w-3" /> Unlimited
            </span>
          )}
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
            <ThreadViewport messages={messages} loading={loading} sessionId={sessionId} />
            <div className="mt-8">
              <ThreadComposer onSend={onSend} onAttach={onAttach} sessionId={sessionId} sending={sending} home />
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
            className="host-no-drag drag-scroll scroll-container min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-y-contain"
          >
            <ThreadViewport messages={messages} loading={loading} sessionId={sessionId} />
          </div>
          <div className="host-no-drag shrink-0 bg-gradient-to-t from-background via-background to-background/80 px-3 pb-3 pt-2 md:px-6 md:pb-5">
            <div className="mx-auto max-w-[880px]">
              <ThreadComposer onSend={onSend} onStop={onStop} onAttach={onAttach} sessionId={sessionId} sending={sending} />
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
