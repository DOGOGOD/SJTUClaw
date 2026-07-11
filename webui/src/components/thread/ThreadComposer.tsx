import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Plus, FolderOpen, FolderSearch } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { fetchWorkspace, setWorkspace, unsetWorkspace } from "@/lib/api";

interface ThreadComposerProps {
  onSend: (message: string) => Promise<void>;
  disabled?: boolean;
  onAttach?: (file: File) => void;
  sessionId?: string | null;
}

export function ThreadComposer({
  onSend,
  disabled = false,
  onAttach,
  sessionId,
}: ThreadComposerProps) {
  const [value, setValue] = useState("");
  const [sending, setSending] = useState(false);
  const [showWsPicker, setShowWsPicker] = useState(false);
  const [wsPath, setWsPath] = useState("");
  const [wsDisplay, setWsDisplay] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const wsRef = useRef<HTMLDivElement>(null);

  // Load workspace on session change
  useEffect(() => {
    const sid = sessionId || "default";
    fetchWorkspace(sid).then((d) => {
      setWsPath(d.workspace || "");
      setWsDisplay(d.workspace ? d.workspace.split("/").pop()?.split("\\").pop() || d.workspace : "");
    }).catch(() => {});
  }, [sessionId]);

  // Close workspace picker on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wsRef.current && !wsRef.current.contains(e.target as Node)) {
        setShowWsPicker(false);
      }
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

  const handleSend = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || disabled || sending) return;
    setSending(true);
    try {
      await onSend(trimmed);
      setValue("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
      }
    } catch {
      // error handling done by parent
    } finally {
      setSending(false);
    }
  }, [value, disabled, sending, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const handleAttach = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file && onAttach) {
        onAttach(file);
      }
      e.target.value = "";
    },
    [onAttach]
  );

  const dirInputRef = useRef<HTMLInputElement>(null);

  const handlePickFolder = useCallback(async () => {
    // Try modern File System Access API first (Chrome/Edge)
    if ("showDirectoryPicker" in window) {
      try {
        const handle = await (window as any).showDirectoryPicker();
        // We can only get the folder name, not the absolute path
        setWsPath((prev) => prev || handle.name);
        return;
      } catch {
        // User cancelled or API failed — fall through to fallback
      }
    }
    // Fallback: use webkitdirectory input
    dirInputRef.current?.click();
  }, []);

  const handleDirInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files;
      if (!files || files.length === 0) return;
      // webkitRelativePath gives "folderName/sub/path/file.txt"
      const relPath = (files[0] as any).webkitRelativePath || "";
      const folderName = relPath.split("/")[0] || "";
      if (folderName && !wsPath) {
        setWsPath(folderName);
      }
      e.target.value = "";
    },
    [wsPath]
  );

  const handleWsSet = async () => {
    const sid = sessionId || "default";
    try {
      await setWorkspace(sid, wsPath);
      setWsDisplay(wsPath.split("/").pop()?.split("\\").pop() || wsPath);
      setShowWsPicker(false);
    } catch {}
  };

  const handleWsUnset = async () => {
    const sid = sessionId || "default";
    try {
      await unsetWorkspace(sid);
      setWsPath("");
      setWsDisplay("");
      setShowWsPicker(false);
    } catch {}
  };

  return (
    <div className="flex items-end gap-2 rounded-xl border border-input bg-card px-3 py-2 transition-colors focus-within:border-ring focus-within:ring-1 focus-within:ring-ring">
      {/* Workspace selector */}
      <div className="relative shrink-0" ref={wsRef}>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setShowWsPicker(!showWsPicker)}
          className={cn(wsDisplay && "text-green-600")}
          title={wsDisplay || "设置 workspace"}
        >
          <FolderOpen className="h-4 w-4" />
        </Button>
        {wsDisplay && (
          <span className="absolute -bottom-1 left-1/2 -translate-x-1/2 text-[8px] text-muted-foreground truncate max-w-[40px]">
            {wsDisplay}
          </span>
        )}
        {showWsPicker && (
          <div className="absolute bottom-full left-0 mb-2 w-72 rounded-lg border border-border bg-popover p-3 shadow-lg z-50">
            <p className="text-xs font-medium mb-2">Workspace 路径</p>
            <div className="flex gap-1 mb-2">
              <Input
                value={wsPath}
                onChange={(e) => setWsPath(e.target.value)}
                placeholder="选择文件夹或输入路径..."
                className="h-7 text-xs flex-1"
                onKeyDown={(e) => e.key === "Enter" && handleWsSet()}
              />
              <input
                ref={dirInputRef}
                type="file"
                className="hidden"
                // @ts-expect-error webkitdirectory is non-standard
                webkitdirectory=""
                directory=""
                onChange={handleDirInputChange}
              />
              <Button
                variant="outline"
                size="sm"
                className="h-7 w-7 p-0 shrink-0"
                onClick={handlePickFolder}
                title="选择文件夹"
              >
                <FolderSearch className="h-3.5 w-3.5" />
              </Button>
            </div>
            <div className="flex gap-1">
              <Button size="sm" className="h-6 text-[10px]" onClick={handleWsSet}>设置</Button>
              {wsPath && (
                <Button variant="ghost" size="sm" className="h-6 text-[10px] text-destructive" onClick={handleWsUnset}>取消</Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Attach button */}
      <input
        ref={fileInputRef}
        type="file"
        className="hidden"
        onChange={handleFileChange}
      />
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={handleAttach}
        className="shrink-0"
        title="添加附件"
      >
        <Plus className="h-4 w-4" />
      </Button>

      {/* Input */}
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          autoResize();
        }}
        onKeyDown={handleKeyDown}
        placeholder="给 SJTUClaw 发送消息..."
        disabled={disabled}
        rows={1}
        className="flex-1 resize-none border-0 bg-transparent py-1 text-sm outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
      />

      {/* Send button */}
      <Button
        size="icon-sm"
        onClick={handleSend}
        disabled={disabled || sending || !value.trim()}
        className={cn(
          "shrink-0 rounded-full transition-all",
          sending && "animate-pulse"
        )}
        title="发送"
      >
        <ArrowUp className="h-4 w-4" />
      </Button>
    </div>
  );
}
