import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Plus, FolderOpen, FolderSearch, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { fetchWorkspace, setWorkspace, unsetWorkspace } from "@/lib/api";

interface ThreadComposerProps {
  onSend: (message: string) => Promise<void>;
  onStop?: () => Promise<void>;
  disabled?: boolean;
  sending?: boolean;
  onAttach?: (file: File) => void;
  sessionId?: string | null;
  home?: boolean;
}

export function ThreadComposer({
  onSend,
  onStop,
  disabled = false,
  sending = false,
  onAttach,
  sessionId,
  home = false,
}: ThreadComposerProps) {
  const [value, setValue] = useState("");
  const [showWsPicker, setShowWsPicker] = useState(false);
  const [wsPath, setWsPath] = useState("");
  const [wsDisplay, setWsDisplay] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const wsRef = useRef<HTMLDivElement>(null);
  const dirInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!sessionId) {
      setWsPath("");
      setWsDisplay("");
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

  const handleSend = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || disabled || sending) return;
    try {
      await onSend(trimmed);
      setValue("");
      if (textareaRef.current) textareaRef.current.style.height = "auto";
    } catch {}
  }, [value, disabled, sending, onSend]);

  const handleStop = useCallback(async () => {
    if (!onStop) return;
    try { await onStop(); } catch {}
  }, [onStop]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (sending && onStop) {
          handleStop();
        } else {
          handleSend();
        }
      }
    },
    [handleSend, handleStop, sending, onStop]
  );

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
    if ("showDirectoryPicker" in window) {
      try { const handle = await (window as any).showDirectoryPicker(); setWsPath((prev) => prev || handle.name); return; } catch {}
    }
    dirInputRef.current?.click();
  }, []);

  const handleDirInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const relPath = (files[0] as any).webkitRelativePath || "";
    const folderName = relPath.split("/")[0] || "";
    if (folderName && !wsPath) setWsPath(folderName);
    e.target.value = "";
  }, [wsPath]);

  const handleWsSet = async () => {
    if (!sessionId) return;
    const sid = sessionId;
    try { await setWorkspace(sid, wsPath); setWsDisplay(wsPath.split("/").pop()?.split("\\").pop() || wsPath); setShowWsPicker(false); } catch {}
  };

  const handleWsUnset = async () => {
    if (!sessionId) return;
    const sid = sessionId;
    try { await unsetWorkspace(sid); setWsPath(""); setWsDisplay(""); setShowWsPicker(false); } catch {}
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
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={home ? "Claw 能帮你做些什么？" : "继续对话..."}
        disabled={disabled}
        rows={1}
        className={cn(
          "max-h-[200px] min-h-10 w-full resize-none border-0 bg-transparent px-1 py-1 text-[15px] leading-6 outline-none placeholder:text-muted-foreground/55 disabled:cursor-not-allowed",
          home && "min-h-[58px]"
        )}
      />

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
              <input
                ref={dirInputRef}
                type="file"
                className="hidden"
                // @ts-expect-error webkitdirectory is non-standard
                webkitdirectory=""
                directory=""
                onChange={handleDirInputChange}
              />
              <Button variant="outline" size="sm" className="h-7 w-7 p-0 shrink-0" onClick={handlePickFolder}><FolderSearch className="h-3 w-3" /></Button>
            </div>
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
