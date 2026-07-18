import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Plus, FolderOpen, FolderSearch, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { fetchWorkspace, pickWorkspace, setWorkspace, unsetWorkspace } from "@/lib/api";

interface ThreadComposerProps {
  onSend: (message: string) => Promise<void>;
  onStop?: () => Promise<void>;
  disabled?: boolean;
  sending?: boolean;
  onAttach?: (file: File) => void;
  sessionId?: string | null;
  messageHistory?: string[];
  home?: boolean;
}

export function ThreadComposer({
  onSend,
  onStop,
  disabled = false,
  sending = false,
  onAttach,
  sessionId,
  messageHistory,
  home = false,
}: ThreadComposerProps) {
  const [value, setValue] = useState("");
  const [showWsPicker, setShowWsPicker] = useState(false);
  const [wsPath, setWsPath] = useState("");
  const [wsDisplay, setWsDisplay] = useState("");
  const [wsError, setWsError] = useState("");
  const [sendError, setSendError] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const wsRef = useRef<HTMLDivElement>(null);
  const valueRef = useRef("");
  const historyRef = useRef(new Map<string, string[]>());
  const historyIndexRef = useRef<number | null>(null);
  const historyDraftRef = useRef("");

  const historyKey = sessionId || "__home__";

  useEffect(() => {
    if (!sessionId) {
      setWsPath("");
      setWsDisplay("");
      setWsError("");
      return;
    }
    fetchWorkspace(sessionId).then((d) => {
      setWsPath(d.workspace || "");
      setWsDisplay(d.workspace ? d.workspace.split("/").pop()?.split("\\").pop() || d.workspace : "");
    }).catch(() => {});
  }, [sessionId]);

  useEffect(() => {
    if (!disabled) textareaRef.current?.focus();
  }, [disabled, sessionId]);

  useEffect(() => {
    historyIndexRef.current = null;
    historyDraftRef.current = "";
    setSendError("");
  }, [sessionId]);

  useEffect(() => {
    if (!messageHistory) return;
    const currentHistory = historyRef.current.get(historyKey) || [];
    const isCurrent = currentHistory.length === messageHistory.length &&
      currentHistory.every((entry, index) => entry === messageHistory[index]);
    if (!isCurrent) {
      historyRef.current.set(historyKey, [...messageHistory]);
      historyIndexRef.current = null;
      historyDraftRef.current = "";
    }
  }, [historyKey, messageHistory]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wsRef.current && !wsRef.current.contains(e.target as Node)) setShowWsPicker(false);
    };
    if (showWsPicker) document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showWsPicker]);

  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, []);

  useEffect(() => { autoResize(); }, [value, autoResize]);

  const updateValue = useCallback((nextValue: string) => {
    valueRef.current = nextValue;
    setValue(nextValue);
  }, []);

  const handleSend = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || disabled || sending) return;

    const history = historyRef.current.get(historyKey) || [];
    const historyEntryIndex = history.length;
    history.push(trimmed);
    historyRef.current.set(historyKey, history);
    historyIndexRef.current = null;
    historyDraftRef.current = "";
    setSendError("");

    // Clear immediately so slow network requests never leave stale text in the composer.
    updateValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";

    try {
      await onSend(trimmed);
    } catch (error) {
      // A rejected send is not part of the sent-message history.
      if (history[historyEntryIndex] === trimmed) history.splice(historyEntryIndex, 1);
      if (valueRef.current === "") updateValue(trimmed);
      setSendError(error instanceof Error ? error.message : "消息发送失败，请重试。");
    }
  }, [value, disabled, sending, historyKey, onSend, updateValue]);

  const handleStop = useCallback(async () => {
    if (!onStop) return;
    try { await onStop(); } catch {}
  }, [onStop]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // Enter confirms an IME candidate before it should be treated as send.
      if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;

      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (sending && onStop) {
          handleStop();
        } else {
          handleSend();
        }
        return;
      }

      if (
        (e.key === "ArrowUp" || e.key === "ArrowDown") &&
        !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey
      ) {
        const history = historyRef.current.get(historyKey) || [];
        if (history.length === 0) return;

        const currentIndex = historyIndexRef.current;
        if (e.key === "ArrowUp") {
          e.preventDefault();
          let nextIndex = currentIndex;
          if (nextIndex === null) {
            historyDraftRef.current = valueRef.current;
            nextIndex = history.length - 1;
          } else if (nextIndex > 0) {
            nextIndex -= 1;
          }
          historyIndexRef.current = nextIndex;
          updateValue(history[nextIndex]);
        } else if (currentIndex !== null) {
          e.preventDefault();
          if (currentIndex < history.length - 1) {
            const nextIndex = currentIndex + 1;
            historyIndexRef.current = nextIndex;
            updateValue(history[nextIndex]);
          } else {
            historyIndexRef.current = null;
            updateValue(historyDraftRef.current);
          }
        }
      }
    },
    [handleSend, handleStop, historyKey, sending, onStop, updateValue]
  );

  const handleValueChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    updateValue(e.target.value);
    historyIndexRef.current = null;
    historyDraftRef.current = "";
    if (sendError) setSendError("");
  }, [sendError, updateValue]);

  const handleAttach = useCallback(() => fileInputRef.current?.click(), []);
  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file && onAttach) onAttach(file);
      e.target.value = "";
    },
    [onAttach]
  );

  const handlePickFolder = useCallback(async () => {
    setWsError("");
    try {
      const result = await pickWorkspace();
      if (result.path) {
        setWsPath(result.path);
        setWsDisplay(result.path.split("/").pop()?.split("\\").pop() || result.path);
      }
    } catch (error) {
      setWsError(error instanceof Error ? error.message : "无法打开本机文件夹选择器。");
    }
  }, []);

  const handleWsSet = async () => {
    if (!sessionId) return;
    const sid = sessionId;
    const path = wsPath.trim();
    if (!path) {
      setWsError("请填写 workspace 的绝对路径。");
      return;
    }
    try {
      await setWorkspace(sid, path);
      setWsPath(path);
      setWsDisplay(path.split("/").pop()?.split("\\").pop() || path);
      setWsError("");
      setShowWsPicker(false);
    } catch (error) {
      setWsError(error instanceof Error ? error.message : "workspace 设置失败，请检查路径是否存在且为文件夹。");
    }
  };

  const handleWsUnset = async () => {
    if (!sessionId) return;
    const sid = sessionId;
    try { await unsetWorkspace(sid); setWsPath(""); setWsDisplay(""); setWsError(""); setShowWsPicker(false); } catch {}
  };

  const hasContent = value.trim().length > 0;

  return (
    <div className={cn(
      "flex flex-col rounded-[20px] border bg-card p-3 transition-[border-color,box-shadow,transform] duration-200 ease-smooth",
      "border-border/85 shadow-[0_8px_30px_hsl(28_18%_20%/0.08)]",
      "focus-within:border-primary/45 focus-within:shadow-[0_12px_38px_hsl(15_45%_35%/0.12)]",
      "dark:bg-card/95 dark:shadow-[0_12px_36px_hsl(25_20%_3%/0.3)]",
      home && "min-h-[118px]"
    )}>
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleValueChange}
        onKeyDown={handleKeyDown}
        placeholder={home ? "Claw 能帮你做些什么？" : "继续对话..."}
        disabled={disabled}
        rows={1}
        className={cn(
          "max-h-[200px] min-h-10 w-full resize-none border-0 bg-transparent px-1 py-1 text-[15px] leading-6 outline-none placeholder:text-muted-foreground/55 disabled:cursor-not-allowed",
          home && "min-h-[58px]"
        )}
      />

      {sendError && <p role="alert" className="px-1 pt-1 text-[11px] leading-relaxed text-destructive">{sendError}</p>}

      <div className="mt-2 flex items-center gap-1.5">
      {/* Workspace selector */}
      <div className="relative shrink-0" ref={wsRef}>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setShowWsPicker(!showWsPicker)}
          disabled={!sessionId}
          className={cn("h-8 w-8 rounded-xl", wsDisplay && "bg-primary/10 text-primary")}
          title={wsDisplay || "未设置 workspace"}
        >
          <FolderOpen className="h-3.5 w-3.5" />
        </Button>
        {showWsPicker && (
          <div className="absolute bottom-full left-0 z-50 mb-2 w-[min(20rem,calc(100vw-2rem))] rounded-2xl border border-border/70 bg-popover/95 p-4 shadow-2xl backdrop-blur-xl animate-enter-scale">
            <p className="mb-1 text-sm font-semibold">Workspace</p>
            <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">设置当前会话允许操作的项目目录。</p>
            <div className="flex gap-1.5 mb-2.5">
              <Input value={wsPath} onChange={(e) => setWsPath(e.target.value)} placeholder="选择文件夹或输入路径..." className="h-7 text-xs flex-1" onKeyDown={(e) => e.key === "Enter" && handleWsSet()} />
              <Button variant="outline" size="sm" className="h-7 w-7 p-0 shrink-0" onClick={handlePickFolder}><FolderSearch className="h-3 w-3" /></Button>
            </div>
            {wsError && <p role="alert" className="mb-2 text-[11px] leading-relaxed text-destructive">{wsError}</p>}
            <div className="flex gap-1.5">
              <Button size="sm" className="h-6 text-[10px]" onClick={handleWsSet}>设置</Button>
              {wsPath && <Button variant="ghost" size="sm" className="h-6 text-[10px] text-destructive" onClick={handleWsUnset}>取消</Button>}
            </div>
          </div>
        )}
      </div>

      {/* Attach */}
      <input ref={fileInputRef} type="file" className="hidden" onChange={handleFileChange} />
      <Button variant="ghost" size="icon-sm" onClick={handleAttach} className="h-8 w-8 shrink-0 rounded-xl" title="添加附件">
        <Plus className="h-3.5 w-3.5" />
      </Button>

      <div className="flex-1" />

      {/* Send / Stop button */}
      {sending && onStop ? (
        <Button
          size="icon-sm"
          onClick={handleStop}
          className={cn(
            "h-8 w-8 shrink-0 rounded-xl transition-[color,background-color,transform] duration-200 ease-smooth",
            "bg-destructive/90 text-destructive-foreground hover:bg-destructive"
          )}
          title="停止生成"
        >
          <Square className="h-3.5 w-3.5 fill-current" />
        </Button>
      ) : (
        <Button
          size="icon-sm"
          onClick={handleSend}
          disabled={disabled || sending || !hasContent}
          className={cn(
            "h-8 w-8 shrink-0 rounded-xl transition-[color,background-color,transform] duration-200 ease-smooth",
            hasContent && !sending
              ? "bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm"
              : "text-muted-foreground/35 bg-transparent hover:bg-transparent",
            sending && "opacity-50"
          )}
          title="发送"
        >
          <ArrowUp className="h-4 w-4" />
        </Button>
      )}
      </div>
    </div>
  );
}
