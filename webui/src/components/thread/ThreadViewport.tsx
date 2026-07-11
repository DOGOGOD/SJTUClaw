import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { PrismAsyncLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";

interface ThreadViewportProps {
  messages: ChatMessage[];
  loading: boolean;
  sessionId: string | null;
}

function CodeBlock({ language, value }: { language: string; value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="group relative my-2">
      <button
        onClick={handleCopy}
        className="absolute right-2 top-2 z-10 rounded p-1 opacity-0 transition-opacity group-hover:opacity-100 bg-secondary hover:bg-accent"
        title="复制"
      >
        {copied ? (
          <Check className="h-3 w-3 text-green-500" />
        ) : (
          <Copy className="h-3 w-3" />
        )}
      </button>
      <SyntaxHighlighter
        language={language || "text"}
        style={oneDark}
        customStyle={{
          margin: 0,
          borderRadius: "0.5rem",
          fontSize: "0.8rem",
        }}
      >
        {value}
      </SyntaxHighlighter>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  if (message.role === "tool") {
    let toolName = "";
    let displayContent = message.content;
    try {
      const parsed = JSON.parse(message.content);
      if (parsed.tool) toolName = parsed.tool;
      displayContent = JSON.stringify(parsed, null, 2);
    } catch {}
    return (
      <div className="my-1 animate-fade-in">
        <details className="group rounded-lg border border-border bg-card">
          <summary className="cursor-pointer px-3 py-1.5 text-xs font-medium text-muted-foreground hover:text-foreground">
            🔧 {toolName || "工具调用"}
          </summary>
          <pre className="max-h-48 overflow-auto border-t border-border px-3 py-2 text-[11px] text-muted-foreground whitespace-pre-wrap font-mono">
            {displayContent.slice(0, 2000)}
          </pre>
        </details>
      </div>
    );
  }

  if (message.role === "system") {
    return (
      <div className="my-1 animate-fade-in">
        <div className="rounded-lg border border-border bg-muted/50 px-3 py-2 text-xs text-muted-foreground whitespace-pre-wrap font-mono">
          ⚡ {message.content}
        </div>
      </div>
    );
  }

  const isUser = message.role === "user";
  const avatar = isUser ? "U" : "C";

  return (
    <div
      className={cn(
        "flex gap-3 py-2 animate-fade-in",
        isUser ? "flex-row-reverse" : ""
      )}
    >
      <div
        className={cn(
          "flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold",
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-amber-800 text-white"
        )}
      >
        {avatar}
      </div>
      <div
        className={cn(
          "min-w-0 max-w-[80%] rounded-lg px-3 py-2 text-sm leading-relaxed",
          isUser ? "bg-primary/10" : "bg-card border border-border"
        )}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown
              remarkPlugins={[remarkGfm, remarkBreaks]}
              components={{
                code({ className, children, ...props }) {
                  const match = /language-(\w+)/.exec(className || "");
                  const value = String(children).replace(/\n$/, "");
                  if (match) {
                    return <CodeBlock language={match[1]} value={value} />;
                  }
                  return (
                    <code className="rounded bg-muted px-1 py-0.5 text-xs font-mono" {...props}>
                      {children}
                    </code>
                  );
                },
                pre({ children }) {
                  return <>{children}</>;
                },
              }}
            >
              {message.content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}

export function ThreadViewport({
  messages,
  loading,
  sessionId,
}: ThreadViewportProps) {
  if (!sessionId) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl text-3xl">
            🦞
          </div>
          <h2 className="text-lg font-semibold">你好，我是 SJTUClaw</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            选择一个已有对话或创建新对话开始吧。
          </p>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-muted-foreground">加载消息中…</p>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl text-3xl">
            🦞
          </div>
          <h2 className="text-lg font-semibold">开始新的对话</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            输入你的问题，SJTUClaw 会帮你找到答案。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-4">
      {messages.map((msg, i) => (
        <MessageBubble key={i} message={msg} />
      ))}
    </div>
  );
}
