import { Children, Component, memo, useCallback, useEffect, useMemo, useRef, useState, lazy, Suspense } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Check, Copy } from "lucide-react";
import { BrandAvatar } from "@/components/BrandAvatar";
import { PetSprite } from "@/components/PetSprite";
import { cn } from "@/lib/utils";
import { useTheme } from "@/hooks/useTheme";
import type { ChatMessage } from "@/lib/types";
import { ToolCallCard, InlineToolCalls } from "./ToolCallCard";

/** Local error boundary so a syntax-highlighter crash doesn't take down the whole chat. */
class CodeBlockErrorBoundary extends Component<{ children: React.ReactNode; fallback: React.ReactNode }, { hasError: boolean }> {
  constructor(props: { children: React.ReactNode; fallback: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}

// Lazy-load the heavy syntax highlighter only when a code block is rendered
const SyntaxHighlighter = lazy(() =>
  import("./CodeHighlighter")
);

type PrismStyle = Record<string, React.CSSProperties>;
const styleCache = new Map<string, PrismStyle>();
async function loadPrismStyle(theme: "light" | "dark"): Promise<PrismStyle> {
  if (styleCache.has(theme)) return styleCache.get(theme)!;
  const m = await import("react-syntax-highlighter/dist/esm/styles/prism");
  const style = theme === "dark" ? m.oneDark : m.oneLight;
  styleCache.set(theme, style);
  return style;
}

interface ThreadViewportProps {
  messages: ChatMessage[];
  loading: boolean;
  sessionId: string | null;
}

function markdownUrlTransform(url: string, key: string): string {
  let decoded = url;
  try { decoded = decodeURIComponent(url); } catch {}
  if (key === "src" && (
    /^file:/i.test(url) || /^[a-z]:[\\/]/i.test(decoded)
  )) return url;
  return defaultUrlTransform(url);
}

function resolveImageSource(source: string, sessionId: string | null): string {
  if (!source || /^(https?:|data:image\/|blob:)/i.test(source) || source.startsWith("/")) {
    return source;
  }
  let localPath = source;
  try { localPath = decodeURIComponent(localPath); } catch {}
  if (/^file:/i.test(localPath)) {
    try { localPath = new URL(localPath).pathname; } catch {}
    if (/^\/[a-z]:/i.test(localPath)) localPath = localPath.slice(1);
  }
  if (!sessionId) return source;
  return `/sessions/${encodeURIComponent(sessionId)}/local-image?path=${encodeURIComponent(localPath)}`;
}

function normalizeMathSegment(segment: string): string {
  let normalized = segment;

  // LLMs commonly emit native LaTeX delimiters. Markdown treats the
  // backslashes as escapes unless we translate them before parsing.
  const inlineOpen = normalized.match(/\\\(/g)?.length || 0;
  const inlineClose = normalized.match(/\\\)/g)?.length || 0;
  if (inlineOpen > 0 && inlineOpen === inlineClose) {
    normalized = normalized.replace(/\\\(/g, "$").replace(/\\\)/g, "$");
  }
  const displayOpen = normalized.match(/\\\[/g)?.length || 0;
  const displayClose = normalized.match(/\\\]/g)?.length || 0;
  if (displayOpen > 0 && displayOpen === displayClose) {
    // A replacement string of "$$" means a single literal "$" in JS;
    // callbacks preserve the two display-math delimiter characters.
    normalized = normalized
      .replace(/\\\[/g, () => "$$")
      .replace(/\\\]/g, () => "$$");
  }

  const delimiters = normalized.match(/\$\$/g)?.length || 0;
  if (delimiters < 2 || delimiters % 2 !== 0) return normalized;

  let result = "";
  let cursor = 0;
  let open = false;
  while (true) {
    const index = normalized.indexOf("$$", cursor);
    if (index < 0) break;
    result += normalized.slice(cursor, index);
    if (!result.endsWith("\n")) result += "\n";
    result += "$$\n";
    if (open) result += "\n";
    open = !open;
    cursor = index + 2;
  }
  return result + normalized.slice(cursor);
}

/** Make same-line/adjacent $$ blocks parseable without touching code samples. */
function normalizeMathMarkdown(markdown: string): string {
  if (!markdown.includes("$$") && !/\\[()[\]]/.test(markdown)) return markdown;
  const code = /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g;
  let result = "";
  let cursor = 0;
  for (const match of markdown.matchAll(code)) {
    const index = match.index ?? 0;
    result += normalizeMathSegment(markdown.slice(cursor, index));
    result += match[0];
    cursor = index + match[0].length;
  }
  return result + normalizeMathSegment(markdown.slice(cursor));
}

function MessageImage({ src, alt, sessionId }: { src?: string; alt?: string; sessionId: string | null }) {
  const [failed, setFailed] = useState(false);
  if (!src) return null;
  const resolved = resolveImageSource(src, sessionId);
  if (failed) {
    return <a href={resolved} target="_blank" rel="noreferrer">无法显示图片：{alt || src}</a>;
  }
  return (
    <a href={resolved} target="_blank" rel="noreferrer" className="block no-underline">
      <img
        src={resolved}
        alt={alt || "消息图片"}
        loading="lazy"
        onError={() => setFailed(true)}
        className="my-2 max-h-[520px] max-w-full rounded-xl border border-border/60 object-contain shadow-sm"
      />
    </a>
  );
}

function imageNameFromLink(children: React.ReactNode): string | null {
  const text = Children.toArray(children)
    .filter((child): child is string => typeof child === "string")
    .join("");
  const match = text.match(/[^\\/\s]+\.(?:png|jpe?g|gif|webp|bmp|avif)$/i);
  return match?.[0] || null;
}

function MessageLink({
  href,
  children,
  sessionId,
  ...props
}: React.AnchorHTMLAttributes<HTMLAnchorElement> & { sessionId: string | null }) {
  const imageName = href && /\/downloads\/[^/]+$/i.test(href)
    ? imageNameFromLink(children)
    : null;
  if (imageName && href) {
    return <MessageImage src={href} alt={imageName} sessionId={sessionId} />;
  }
  return (
    <a href={href} target="_blank" rel="noreferrer" {...props}>
      {children}
    </a>
  );
}

const USER_AVATARS = [
  { id: "initial", glyph: "U", label: "字母 U" },
  { id: "person", glyph: "👤", label: "人物" },
  { id: "cat", glyph: "🐱", label: "猫咪" },
  { id: "dog", glyph: "🐶", label: "小狗" },
  { id: "fox", glyph: "🦊", label: "狐狸" },
  { id: "panda", glyph: "🐼", label: "熊猫" },
] as const;

type UserAvatarId = (typeof USER_AVATARS)[number]["id"];
type UserAvatarSelection = UserAvatarId | "custom";

const USER_AVATAR_STORAGE_KEY = "sjtuclaw.user-avatar";
const USER_AVATAR_IMAGE_STORAGE_KEY = "sjtuclaw.user-avatar-image";
const USER_AVATAR_MAX_FILE_BYTES = 8 * 1024 * 1024;
const USER_AVATAR_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/bmp",
]);

function loadUserAvatarImage(): string {
  if (typeof window === "undefined") return "";
  try {
    const stored = window.localStorage.getItem(USER_AVATAR_IMAGE_STORAGE_KEY) || "";
    return stored.startsWith("data:image/") ? stored : "";
  } catch {}
  return "";
}

function loadUserAvatar(): UserAvatarSelection {
  if (typeof window === "undefined") return "initial";
  try {
    const stored = window.localStorage.getItem(USER_AVATAR_STORAGE_KEY);
    if (stored === "custom" && loadUserAvatarImage()) return "custom";
    if (USER_AVATARS.some((avatar) => avatar.id === stored)) {
      return stored as UserAvatarId;
    }
  } catch {}
  return "initial";
}

function prepareUserAvatarImage(file: File): Promise<string> {
  if (!USER_AVATAR_IMAGE_TYPES.has(file.type)) {
    return Promise.reject(new Error("请选择 PNG、JPG、WebP、GIF 或 BMP 图片"));
  }
  if (file.size > USER_AVATAR_MAX_FILE_BYTES) {
    return Promise.reject(new Error("图片不能超过 8 MB"));
  }

  return new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      try {
        const sourceSize = Math.min(image.naturalWidth, image.naturalHeight);
        if (!sourceSize) throw new Error("无法读取图片尺寸");
        const sourceX = (image.naturalWidth - sourceSize) / 2;
        const sourceY = (image.naturalHeight - sourceSize) / 2;
        const canvas = document.createElement("canvas");
        canvas.width = 256;
        canvas.height = 256;
        const context = canvas.getContext("2d");
        if (!context) throw new Error("浏览器不支持图片处理");
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = "high";
        context.drawImage(
          image,
          sourceX,
          sourceY,
          sourceSize,
          sourceSize,
          0,
          0,
          256,
          256
        );
        resolve(canvas.toDataURL("image/webp", 0.88));
      } catch (error) {
        reject(error);
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    };
    image.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error("无法读取该图片"));
    };
    image.src = objectUrl;
  });
}

function UserAvatar({
  avatarId,
  customImage,
  onChange,
  onCustomImage,
}: {
  avatarId: UserAvatarSelection;
  customImage: string;
  onChange: (avatarId: UserAvatarSelection) => void;
  onCustomImage: (dataUrl: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState("");
  const [menuPosition, setMenuPosition] = useState({ left: 8, top: 8 });
  const containerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const selected = USER_AVATARS.find((avatar) => avatar.id === avatarId) || USER_AVATARS[0];
  const selectedLabel = avatarId === "custom" && customImage ? "本地图片" : selected.label;

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !containerRef.current?.contains(target)
        && !menuRef.current?.contains(target)
      ) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const closeOnViewportChange = () => setOpen(false);
    document.addEventListener("pointerdown", closeOnOutsidePress);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePress);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [open]);

  return (
    <div ref={containerRef} className="relative mt-0.5 shrink-0">
      <button
        type="button"
        className="flex h-9 w-9 items-center justify-center overflow-hidden rounded-xl bg-foreground/10 text-sm font-semibold text-foreground/65 ring-1 ring-border/60 transition-colors hover:bg-foreground/[0.14] select-none"
        onContextMenu={(event) => {
          event.preventDefault();
          const rect = event.currentTarget.getBoundingClientRect();
          const menuWidth = 176;
          const menuHeight = customImage ? 268 : 216;
          const viewportPadding = 8;
          const left = Math.max(
            viewportPadding,
            Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - viewportPadding)
          );
          const belowTop = rect.bottom + viewportPadding;
          const aboveTop = rect.top - menuHeight - viewportPadding;
          let top: number;
          if (window.innerHeight - belowTop >= menuHeight) top = belowTop;
          else if (aboveTop >= viewportPadding) top = aboveTop;
          else {
            top = Math.max(
              viewportPadding,
              Math.min(rect.top - menuHeight / 2, window.innerHeight - menuHeight - viewportPadding)
            );
          }
          setMenuPosition({ left, top });
          setOpen(true);
        }}
        title="右击切换头像"
        aria-label={`用户头像：${selectedLabel}，右击切换`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {avatarId === "custom" && customImage ? (
          <img src={customImage} alt="" className="h-full w-full object-cover" draggable={false} />
        ) : (
          <span aria-hidden="true">{selected.glyph}</span>
        )}
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          role="menu"
          aria-label="选择用户头像"
          className="fixed z-[100] grid max-h-[calc(100vh-1rem)] w-44 grid-cols-3 gap-1.5 overflow-y-auto rounded-2xl border border-border/70 bg-popover/95 p-2.5 shadow-xl backdrop-blur-xl animate-enter-scale"
          style={{ left: menuPosition.left, top: menuPosition.top }}
          onContextMenu={(event) => event.preventDefault()}
        >
          {USER_AVATARS.map((avatar) => (
            <button
              key={avatar.id}
              type="button"
              role="menuitemradio"
              aria-checked={avatar.id === avatarId}
              title={avatar.label}
              className={cn(
                "flex h-11 items-center justify-center rounded-xl text-lg transition-colors hover:bg-secondary",
                avatar.id === avatarId && "bg-primary/10 ring-1 ring-primary/30"
              )}
              onClick={() => {
                onChange(avatar.id);
                setOpen(false);
              }}
            >
              <span aria-hidden="true">{avatar.glyph}</span>
            </button>
          ))}
          {customImage && (
            <button
              type="button"
              role="menuitemradio"
              aria-checked={avatarId === "custom"}
              title="本地图片"
              className={cn(
                "flex h-11 items-center justify-center overflow-hidden rounded-xl transition-colors hover:bg-secondary",
                avatarId === "custom" && "bg-primary/10 ring-1 ring-primary/30"
              )}
              onClick={() => {
                onChange("custom");
                setOpen(false);
              }}
            >
              <img src={customImage} alt="" className="h-full w-full object-cover" />
            </button>
          )}
          <button
            type="button"
            className="col-span-3 mt-1 flex h-9 items-center justify-center rounded-xl border border-dashed border-border/80 px-3 text-xs font-medium text-muted-foreground transition-colors hover:border-primary/40 hover:bg-primary/[0.04] hover:text-foreground disabled:opacity-50"
            disabled={importing}
            onClick={() => fileInputRef.current?.click()}
          >
            {importing ? "正在处理图片..." : "导入本地图片"}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
            className="hidden"
            aria-label="导入本地头像图片"
            onChange={async (event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (!file) return;
              setImporting(true);
              setImportError("");
              try {
                const dataUrl = await prepareUserAvatarImage(file);
                onCustomImage(dataUrl);
                setOpen(false);
              } catch (error) {
                setImportError(error instanceof Error ? error.message : "图片导入失败");
              } finally {
                setImporting(false);
              }
            }}
          />
          {importError && (
            <p className="col-span-3 px-1 text-[10px] leading-relaxed text-destructive" role="alert">
              {importError}
            </p>
          )}
        </div>,
        document.body
      )}
    </div>
  );
}

const CodeBlock = memo(function CodeBlock({
  language,
  value,
}: {
  language: string;
  value: string;
}) {
  const [copied, setCopied] = useState(false);
  const { theme } = useTheme();
  const [style, setStyle] = useState<PrismStyle | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadPrismStyle(theme).then((s) => {
      if (!cancelled) setStyle(s);
    });
    return () => { cancelled = true; };
  }, [theme]);

  const fallback = (
    <pre className="my-4 rounded-xl border border-border/70 bg-secondary/70 p-4 text-[12px] font-mono overflow-auto text-foreground/90">
      {value}
    </pre>
  );

  return (
    <div className="group relative my-4 overflow-hidden rounded-xl border border-border/60 bg-card shadow-sm">
      <button
        onClick={() => {
          navigator.clipboard.writeText(value).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
        className="absolute right-2 top-2 z-10 rounded-md p-1.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100 bg-secondary/80 hover:bg-secondary text-muted-foreground hover:text-foreground"
        title="复制"
      >
        {copied ? (
          <Check className="h-3 w-3 text-green-500" />
        ) : (
          <Copy className="h-3 w-3" />
        )}
      </button>
      <CodeBlockErrorBoundary fallback={fallback}>
        <Suspense fallback={fallback}>
          <SyntaxHighlighter
            language={language || "text"}
            style={style || ({} as PrismStyle)}
            customStyle={{
              margin: 0,
              borderRadius: 0,
              fontSize: "0.8rem",
            }}
          >
            {value}
          </SyntaxHighlighter>
        </Suspense>
      </CodeBlockErrorBoundary>
    </div>
  );
});

const MessageBubble = memo(function MessageBubble({
  message,
  sessionId,
  userAvatarId,
  userAvatarImage,
  onUserAvatarChange,
  onUserAvatarImageChange,
}: {
  message: ChatMessage;
  sessionId: string | null;
  index: number;
  userAvatarId: UserAvatarSelection;
  userAvatarImage: string;
  onUserAvatarChange: (avatarId: UserAvatarSelection) => void;
  onUserAvatarImageChange: (dataUrl: string) => void;
}) {
  // Tool call result messages display as enhanced cards
  if (message.role === "tool") {
    let toolName = message.name || "";
    let ok = true;
    let result: string | null = message.content;
    let error: string | null = null;
    try {
      const parsed = JSON.parse(message.content);
      if (parsed.tool && !toolName) toolName = parsed.tool;
      if (parsed.ok === false) {
        ok = false;
        error = parsed.result || "工具执行失败";
      }
      if (parsed.result && ok)
        result =
          typeof parsed.result === "string"
            ? parsed.result
            : JSON.stringify(parsed.result, null, 2);
    } catch {}

    const live: import("@/lib/types").LiveToolCall = {
      callId: message.tool_call_id || `tool_${message.timestamp || "0"}`,
      toolName: toolName || "工具调用",
      args: {},
      status: ok ? "ok" : "error",
      result: ok ? result : null,
      error: !ok ? error || "未知错误" : null,
      durationMs: null,
      startedAt: message.timestamp || "",
      iteration: 0,
    };

    return (
      <div className="my-1 message-row">
        <ToolCallCard toolCall={live} />
      </div>
    );
  }

  // Assistant messages with tool_calls display an inline tool call indicator
  if (
    message.role === "assistant" &&
    message.tool_calls &&
    message.tool_calls.length > 0
  ) {
    const toolCalls = message.tool_calls.map((tc) => ({
      id: tc.id,
      function: { name: tc.function.name, arguments: tc.function.arguments },
    }));
    return (
      <div className="my-1 message-row">
        <InlineToolCalls toolCalls={toolCalls} />
      </div>
    );
  }

  if (message.role === "system") {
    return (
      <div className="my-1 flex justify-center message-row">
        <span className="rounded-full bg-muted/50 px-3 py-1 text-[11px] text-muted-foreground/60">
          {message.content}
        </span>
      </div>
    );
  }

  const isUser = message.role === "user";
  const markdownContent = normalizeMathMarkdown(message.content);

  return (
    <div className={cn("flex gap-3 py-4 message-row md:gap-4", isUser ? "justify-end" : "")}>
      {!isUser && (
        <BrandAvatar className="mt-0.5 h-9 w-9" fullCharacter />
      )}
      <div
        className={cn(
          "min-w-0 max-w-[82%] md:max-w-[78%]",
          isUser && "flex flex-col items-end"
        )}
      >
        {isUser ? (
          <div className="flex min-h-10 items-center rounded-2xl rounded-br-md border border-border/50 bg-secondary/75 px-4 py-2.5 text-[14px] leading-relaxed text-foreground/90 shadow-sm">
            <div className="user-message-content prose prose-sm dark:prose-invert max-w-none prose-p:leading-relaxed">
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkBreaks, remarkMath]}
                rehypePlugins={[rehypeKatex]}
                urlTransform={markdownUrlTransform}
                components={{
                  img({ src, alt }) {
                    return <MessageImage src={src} alt={alt} sessionId={sessionId} />;
                  },
                  a({ href, children, ...props }) {
                    return <MessageLink href={href} children={children} sessionId={sessionId} {...props} />;
                  },
                  code({ className, children, ...props }) {
                    const match = /language-(\w+)/.exec(className || "");
                    const value = String(children).replace(/\n$/, "");
                    if (match)
                      return (
                        <CodeBlock language={match[1]} value={value} />
                      );
                    return (
                      <code
                        className="rounded-md bg-background/70 px-1.5 py-0.5 text-[12px] font-mono font-medium"
                        {...props}
                      >
                        {children}
                      </code>
                    );
                  },
                  pre({ children }) {
                    return <>{children}</>;
                  },
                }}
              >
                {markdownContent}
              </ReactMarkdown>
            </div>
          </div>
        ) : (
          <div className={cn(
            "w-full pt-0.5 text-[14px] leading-relaxed text-foreground/90",
            message.command && "rounded-xl border border-border/60 bg-card/65 px-5 py-4 shadow-sm"
          )}>
            <div className="prose prose-sm dark:prose-invert max-w-none prose-p:leading-relaxed">
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkBreaks, remarkMath]}
                rehypePlugins={[rehypeKatex]}
                urlTransform={markdownUrlTransform}
                components={{
                  img({ src, alt }) {
                    return <MessageImage src={src} alt={alt} sessionId={sessionId} />;
                  },
                  a({ href, children, ...props }) {
                    return <MessageLink href={href} children={children} sessionId={sessionId} {...props} />;
                  },
                  code({ className, children, ...props }) {
                    const match = /language-(\w+)/.exec(className || "");
                    const value = String(children).replace(/\n$/, "");
                    if (match)
                      return (
                        <CodeBlock language={match[1]} value={value} />
                      );
                    return (
                      <code
                        className="rounded-md bg-secondary/80 px-1.5 py-0.5 text-[12px] font-mono font-medium"
                        {...props}
                      >
                        {children}
                      </code>
                    );
                  },
                  pre({ children }) {
                    return <>{children}</>;
                  },
                }}
              >
                {markdownContent}
              </ReactMarkdown>
            </div>
          </div>
        )}
      </div>
      {isUser && (
        <UserAvatar
          avatarId={userAvatarId}
          customImage={userAvatarImage}
          onChange={onUserAvatarChange}
          onCustomImage={onUserAvatarImageChange}
        />
      )}
    </div>
  );
});

export function ThreadViewport({ messages, loading, sessionId }: ThreadViewportProps) {
  const [userAvatarId, setUserAvatarId] = useState<UserAvatarSelection>(loadUserAvatar);
  const [userAvatarImage, setUserAvatarImage] = useState(loadUserAvatarImage);
  const handleUserAvatarChange = useCallback((avatarId: UserAvatarSelection) => {
    setUserAvatarId(avatarId);
    try {
      window.localStorage.setItem(USER_AVATAR_STORAGE_KEY, avatarId);
    } catch {}
  }, []);
  const handleUserAvatarImageChange = useCallback((dataUrl: string) => {
    setUserAvatarImage(dataUrl);
    setUserAvatarId("custom");
    try {
      window.localStorage.setItem(USER_AVATAR_IMAGE_STORAGE_KEY, dataUrl);
      window.localStorage.setItem(USER_AVATAR_STORAGE_KEY, "custom");
    } catch {}
  }, []);
  const welcome = useMemo(() => {
    const greetings = [
      "今天，我们从哪里开始？",
      "有什么想一起完成的吗？",
      "把正在想的事交给我吧。",
      "今天想探索什么？",
      "准备好做点什么了吗？",
      "有什么值得认真想一想？",
      "想梳理思绪，或是创造些什么？",
      "What would you like to work on today?",
      "需要我帮你梳理难题、撰写内容吗？",
      "随意说说你的想法，我随时倾听。",
      "Ready to brainstorm new ideas?",
      "今天有什么计划想落地实现？",
      "想写文案、解问题还是构思方案？",
      "Share your thoughts, let's figure it out together.",
      "有藏在心底的构思，不妨讲给我听。",
      "What topic shall we dive into first?",
      "需要复盘、规划，或是自由创作？",
      "放下顾虑，把所有疑问都抛给我。",
      "Let’s turn your ideas into clear words.",
      "今天打算攻克哪一件棘手的事？"
    ];
    return greetings[Math.floor(Math.random() * greetings.length)];
  }, [sessionId]);

  if (!sessionId) {
    return (
      <div className="w-full text-center animate-enter-up">
        <PetSprite className="mx-auto mb-5" />
        <h2 className="font-display text-[clamp(1.8rem,4vw,2.45rem)] font-normal leading-tight tracking-[-0.035em] text-foreground/95">
          {welcome}
        </h2>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-[880px] space-y-6 px-5 py-10 md:px-8">
        <div className="flex gap-4"><div className="skeleton h-8 w-8 rounded-xl"/><div className="flex-1 space-y-2"><div className="skeleton h-3 w-3/5 rounded-md"/><div className="skeleton h-3 w-4/5 rounded-md"/><div className="skeleton h-3 w-2/5 rounded-md"/></div></div>
        <div className="ml-auto skeleton h-16 w-2/5 rounded-2xl" />
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6 py-12">
        <div className="w-full max-w-xl">
          <BrandAvatar className="mb-5 h-12 w-12" fullCharacter />
          <h2 className="font-display text-2xl font-normal tracking-[-0.03em]">今天想完成什么？</h2>
          <p className="mt-2 text-[14px] leading-relaxed text-muted-foreground">
            描述目标，SJTUClaw 会规划步骤、调用工具并持续汇报进度。
          </p>
        </div>
      </div>
    );
  }

  // Use stable keys: prefer tool_call_id > timestamp+role+content hash
  const getMsgKey = (msg: ChatMessage, i: number): string => {
    if (msg.tool_call_id) return msg.tool_call_id;
    if (msg.timestamp) return `${msg.role}-${msg.timestamp}-${i}`;
    return `${msg.role}-${i}`;
  };

  return (
    <div className="mx-auto w-full max-w-[880px] px-5 py-6 md:px-8 md:py-8">
      {messages.map((msg, i) => (
        <MessageBubble
          key={getMsgKey(msg, i)}
          message={msg}
          sessionId={sessionId}
          index={i}
          userAvatarId={userAvatarId}
          userAvatarImage={userAvatarImage}
          onUserAvatarChange={handleUserAvatarChange}
          onUserAvatarImageChange={handleUserAvatarImageChange}
        />
      ))}
    </div>
  );
}
