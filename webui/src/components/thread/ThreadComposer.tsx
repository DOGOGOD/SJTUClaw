import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, FileText, Plus, FolderOpen, FolderSearch, ImageIcon, Square, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { fetchWorkspace, pickWorkspace, setWorkspace, unsetWorkspace } from "@/lib/api";

interface ThreadComposerProps {
  onSend: (message: string, attachments?: File[]) => Promise<void>;
  onStop?: () => Promise<void>;
  disabled?: boolean;
  sending?: boolean;
  sessionId?: string | null;
  messageHistory?: string[];
  workspaceRefreshToken?: number;
  home?: boolean;
  onCreateWorkspaceSession?: (path: string) => Promise<void>;
}

interface PendingAttachment {
  file: File;
  previewUrl: string;
  previewableImage: boolean;
}

const MAX_ATTACHMENTS = 4;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const PREVIEWABLE_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/bmp",
  "image/avif",
]);

export function ThreadComposer({
  onSend,
  onStop,
  disabled = false,
  sending = false,
  sessionId,
  messageHistory,
  workspaceRefreshToken = 0,
  home = false,
  onCreateWorkspaceSession,
}: ThreadComposerProps) {
  const [value, setValue] = useState("");
  const [showWsPicker, setShowWsPicker] = useState(false);
  const [wsPath, setWsPath] = useState("");
  const [savedWsPath, setSavedWsPath] = useState("");
  const [wsError, setWsError] = useState("");
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [sendError, setSendError] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const wsRef = useRef<HTMLDivElement>(null);
  const valueRef = useRef("");
  const historyRef = useRef(new Map<string, string[]>());
  const historyIndexRef = useRef<number | null>(null);
  const historyDraftRef = useRef("");
  const pendingAttachmentsRef = useRef<PendingAttachment[]>([]);
  const workspaceRequestIdRef = useRef(0);

  const historyKey = sessionId || "__home__";
  const wsDisplay = savedWsPath
    ? savedWsPath.split("/").pop()?.split("\\").pop() || savedWsPath
    : "";
  const workspaceDraftChanged = wsPath.trim() !== savedWsPath;

  useEffect(() => {
    const requestId = ++workspaceRequestIdRef.current;
    setWsPath("");
    setSavedWsPath("");
    setWsError("");
    if (!sessionId) {
      return;
    }
    fetchWorkspace(sessionId).then((d) => {
      if (workspaceRequestIdRef.current !== requestId) return;
      const workspace = d.workspace || "";
      setWsPath(workspace);
      setSavedWsPath(workspace);
    }).catch(() => {});
  }, [sessionId, workspaceRefreshToken]);

  useEffect(() => {
    if (!disabled) textareaRef.current?.focus();
  }, [disabled, sessionId]);

  useEffect(() => {
    historyIndexRef.current = null;
    historyDraftRef.current = "";
    setSendError("");
    setPendingAttachments((current) => {
      current.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
      return [];
    });
  }, [sessionId]);

  useEffect(() => {
    pendingAttachmentsRef.current = pendingAttachments;
  }, [pendingAttachments]);

  useEffect(() => () => {
    pendingAttachmentsRef.current.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
  }, []);

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
      if (wsRef.current && !wsRef.current.contains(e.target as Node)) {
        setWsPath(savedWsPath);
        setWsError("");
        setShowWsPicker(false);
      }
    };
    if (showWsPicker) document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [savedWsPath, showWsPicker]);

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

  const addPendingAttachments = useCallback((files: File[]) => {
    const supported = files.filter((file) => (
      file.size <= (PREVIEWABLE_IMAGE_TYPES.has(file.type) ? MAX_IMAGE_BYTES : MAX_FILE_BYTES)
    ));
    setPendingAttachments((current) => {
      const available = Math.max(0, MAX_ATTACHMENTS - current.length);
      const accepted = supported.slice(0, available);
      if (supported.length !== files.length) {
        setSendError("图片不能超过 20 MB，其他文件不能超过 50 MB。");
      } else if (supported.length > available) {
        setSendError(`每条消息最多添加 ${MAX_ATTACHMENTS} 个附件。`);
      } else {
        setSendError("");
      }
      return [
        ...current,
        ...accepted.map((file) => {
          const previewableImage = PREVIEWABLE_IMAGE_TYPES.has(file.type);
          return {
          file,
          previewableImage,
          previewUrl: previewableImage && typeof URL.createObjectURL === "function"
            ? URL.createObjectURL(file)
            : "",
          };
        }),
      ];
    });
  }, []);

  const handleSend = useCallback(async () => {
    const trimmed = value.trim();
    if ((!trimmed && pendingAttachments.length === 0) || disabled || sending) return;

    const history = historyRef.current.get(historyKey) || [];
    const historyEntryIndex = trimmed ? history.length : -1;
    if (trimmed) history.push(trimmed);
    historyRef.current.set(historyKey, history);
    historyIndexRef.current = null;
    historyDraftRef.current = "";
    setSendError("");

    // Clear immediately so slow network requests never leave stale text in the composer.
    updateValue("");
    const sentAttachments = pendingAttachments;
    setPendingAttachments([]);
    if (textareaRef.current) textareaRef.current.style.height = "auto";

    try {
      const files = sentAttachments.map((item) => item.file);
      if (files.length > 0) await onSend(trimmed, files);
      else await onSend(trimmed);
      sentAttachments.forEach((item) => item.previewUrl && URL.revokeObjectURL(item.previewUrl));
    } catch (error) {
      // A rejected send is not part of the sent-message history.
      if (historyEntryIndex >= 0 && history[historyEntryIndex] === trimmed) history.splice(historyEntryIndex, 1);
      if (valueRef.current === "") updateValue(trimmed);
      setPendingAttachments((current) => {
        const combined = [...sentAttachments, ...current];
        combined.slice(MAX_ATTACHMENTS).forEach((item) => (
          item.previewUrl && URL.revokeObjectURL(item.previewUrl)
        ));
        return combined.slice(0, MAX_ATTACHMENTS);
      });
      setSendError(error instanceof Error ? error.message : "消息发送失败，请重试。");
    }
  }, [value, pendingAttachments, disabled, sending, historyKey, onSend, updateValue]);

  const handleStop = useCallback(async () => {
    if (!onStop) return;
    setSendError("");
    try {
      await onStop();
    } catch (error) {
      setSendError(error instanceof Error ? error.message : "停止任务失败，请重试。");
    }
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
      const files = Array.from(e.target.files || []);
      if (files.length > 0) addPendingAttachments(files);
      e.target.value = "";
    },
    [addPendingAttachments]
  );

  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const itemFiles = Array.from(e.clipboardData.items || [])
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter((file): file is File => file !== null);
      const files = itemFiles.length > 0
        ? itemFiles
        : Array.from(e.clipboardData.files || []);

      if (files.length === 0) return;
      // A file paste is an attachment action. Prevent the browser from also
      // inserting a filename or other clipboard representation into the draft.
      e.preventDefault();
      addPendingAttachments(files);
    },
    [addPendingAttachments]
  );

  const removePendingAttachment = useCallback((index: number) => {
    setPendingAttachments((current) => {
      const removed = current[index];
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
      return current.filter((_, itemIndex) => itemIndex !== index);
    });
  }, []);

  const handlePickFolder = useCallback(async () => {
    setWsError("");
    setWorkspaceBusy(true);
    try {
      const result = await pickWorkspace();
      if (result.path) {
        setWsPath(result.path);
      }
    } catch (error) {
      setWsError(error instanceof Error ? error.message : "无法打开本机文件夹选择器。");
    } finally {
      setWorkspaceBusy(false);
    }
  }, []);

  const handleWsSet = async () => {
    const path = wsPath.trim();
    if (!path) {
      setWsError("请填写 workspace 的绝对路径。");
      return;
    }
    if (!sessionId && !onCreateWorkspaceSession) {
      setWsError("无法为新会话创建 workspace，请刷新页面后重试。");
      return;
    }
    setWorkspaceBusy(true);
    try {
      ++workspaceRequestIdRef.current;
      if (sessionId) {
        await setWorkspace(sessionId, path);
      } else {
        await onCreateWorkspaceSession!(path);
      }
      setWsPath(path);
      setSavedWsPath(path);
      setWsError("");
      setShowWsPicker(false);
    } catch (error) {
      setWsError(error instanceof Error ? error.message : "workspace 设置失败，请检查路径是否存在且为文件夹。");
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const handleWsUnset = async () => {
    if (!sessionId) {
      setWsPath("");
      setWsError("");
      setShowWsPicker(false);
      return;
    }
    const sid = sessionId;
    setWorkspaceBusy(true);
    try {
      ++workspaceRequestIdRef.current;
      await unsetWorkspace(sid);
      setWsPath("");
      setSavedWsPath("");
      setWsError("");
      setShowWsPicker(false);
    } catch (error) {
      setWsError(error instanceof Error ? error.message : "取消 workspace 失败，请重试。");
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const handleWsCancelOrUnset = async () => {
    if (workspaceDraftChanged) {
      setWsPath(savedWsPath);
      setWsError("");
      setShowWsPicker(false);
      return;
    }
    await handleWsUnset();
  };

  const hasContent = value.trim().length > 0 || pendingAttachments.length > 0;

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
        onPaste={handlePaste}
        placeholder="Chat or Work With Claw"
        disabled={disabled}
        rows={1}
        className={cn(
          "max-h-[200px] min-h-10 w-full resize-none border-0 bg-transparent px-1 py-1 text-[15px] leading-6 outline-none placeholder:text-muted-foreground/55 disabled:cursor-not-allowed",
          home && "min-h-[58px]"
        )}
      />

      {pendingAttachments.length > 0 && (
        <div className="flex gap-2 overflow-x-auto px-1 pt-2" aria-label="待发送附件">
          {pendingAttachments.map((item, index) => (
            <div key={`${item.file.name}-${item.file.lastModified}-${index}`} className="relative flex h-16 w-40 shrink-0 items-center gap-2 overflow-hidden rounded-xl border border-border/80 bg-muted/50 p-2 pr-7">
              {item.previewableImage && item.previewUrl ? (
                <img src={item.previewUrl} alt={item.file.name || `图片 ${index + 1}`} className="h-12 w-12 shrink-0 rounded-lg object-cover" />
              ) : item.previewableImage ? (
                <ImageIcon className="h-5 w-5 shrink-0 text-muted-foreground" />
              ) : (
                <FileText className="h-5 w-5 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 truncate text-[11px] text-foreground/80" title={item.file.name}>
                {item.file.name || `附件 ${index + 1}`}
              </span>
              <button
                type="button"
                aria-label={`移除附件 ${index + 1}`}
                onClick={() => removePendingAttachment(index)}
                className="absolute right-1 top-1 grid h-5 w-5 place-items-center rounded-full bg-black/65 text-white hover:bg-black/80"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {sendError && <p role="alert" className="px-1 pt-1 text-[11px] leading-relaxed text-destructive">{sendError}</p>}

      <div className="mt-2 flex items-center gap-1.5">
      {/* Workspace selector */}
      <div className="relative shrink-0" ref={wsRef}>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => {
            setWsPath(savedWsPath);
            setWsError("");
            setShowWsPicker(!showWsPicker);
          }}
          disabled={workspaceBusy || (!sessionId && !onCreateWorkspaceSession)}
          className={cn("h-8 w-8 rounded-xl", wsDisplay && "bg-primary/10 text-primary")}
          title={wsDisplay || (home ? "选择工作区" : "未设置 workspace")}
          aria-label={wsDisplay ? `当前工作区：${wsDisplay}` : "选择工作区"}
        >
          <FolderOpen className="h-3.5 w-3.5" />
        </Button>
        {showWsPicker && (
          <div className="absolute bottom-full left-0 z-50 mb-2 w-[min(20rem,calc(100vw-2rem))] rounded-2xl border border-border/70 bg-popover/95 p-4 shadow-2xl backdrop-blur-xl animate-enter-scale">
            <p className="mb-1 text-sm font-semibold">Workspace</p>
            <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">
              {sessionId
                ? "设置当前会话允许操作的项目目录。"
                : "选择目录后将创建新会话，并将该目录设为工作区。"}
            </p>
            <div className="flex gap-1.5 mb-2.5">
              <Input
                value={wsPath}
                onChange={(e) => setWsPath(e.target.value)}
                placeholder="选择文件夹或输入路径..."
                className="h-7 text-xs flex-1"
                onKeyDown={(e) => e.key === "Enter" && handleWsSet()}
                aria-label="workspace 路径"
                disabled={workspaceBusy}
              />
              <Button
                variant="outline"
                size="sm"
                className="h-7 w-7 p-0 shrink-0"
                onClick={handlePickFolder}
                aria-label="浏览工作区"
                title="浏览工作区"
                disabled={workspaceBusy}
              >
                <FolderSearch className="h-3 w-3" />
              </Button>
            </div>
            {wsError && <p role="alert" className="mb-2 text-[11px] leading-relaxed text-destructive">{wsError}</p>}
            <div className="flex gap-1.5">
              <Button size="sm" className="h-6 text-[10px]" onClick={handleWsSet} disabled={workspaceBusy}>
                {workspaceBusy ? "设置中" : "设置"}
              </Button>
              {(wsPath || savedWsPath) && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 text-[10px] text-destructive"
                  onClick={handleWsCancelOrUnset}
                  disabled={workspaceBusy}
                >
                  取消
                </Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Attach */}
      <input ref={fileInputRef} type="file" multiple className="hidden" aria-label="选择附件" onChange={handleFileChange} />
      <Button variant="ghost" size="icon-sm" onClick={handleAttach} className="h-8 w-8 shrink-0 rounded-xl" title="添加附件（图片或文件）">
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
