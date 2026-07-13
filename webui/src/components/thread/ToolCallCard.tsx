import { memo, useState } from "react";
import { Check, X, Loader2, ChevronDown, ChevronRight, Clock, Wrench, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LiveToolCall } from "@/lib/types";

interface ToolCallCardProps {
  toolCall: LiveToolCall;
}

/** Format arguments for display and truncate long values. */
function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args).map(([k, v]) => {
    const valStr = typeof v === "string" ? v : JSON.stringify(v);
    const display = valStr.length > 200 ? valStr.slice(0, 200) + "…" : valStr;
    return `${k}: ${display}`;
  });
  return entries.join("\n");
}

/** Format result for display. */
function formatResult(result: string | null): string {
  if (!result) return "(空)";
  return result.length > 3000 ? result.slice(0, 3000) + "\n…(截断)" : result;
}

export const ToolCallCard = memo(function ToolCallCard({ toolCall }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [showArgs, setShowArgs] = useState(false);
  const [showResult, setShowResult] = useState(false);

  const isRunning = toolCall.status === "running";
  const isOk = toolCall.status === "ok";
  const isError = toolCall.status === "error";
  const args = toolCall.args ?? {};

  return (
    <div
      className={cn(
        "my-2 animate-enter-up overflow-hidden rounded-xl border bg-card/55 transition-colors duration-200",
        isRunning && "border-primary/35",
        isOk && "border-border/70",
        isError && "border-destructive/35",
      )}
    >
      {/* Header row is always visible */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left hover:bg-secondary/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/35"
      >
        {/* Status icon */}
        {isRunning ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />
        ) : isOk ? (
          <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
        ) : (
          <X className="h-3.5 w-3.5 shrink-0 text-red-500 dark:text-red-400" />
        )}

        {/* Tool name */}
        <span className="flex items-center gap-1.5 text-[12px] font-medium text-foreground/85">
          <Wrench className="h-3 w-3 text-muted-foreground/50" />
          {toolCall.toolName}
        </span>

        {/* Duration */}
        {toolCall.durationMs != null && (
          <span className="flex items-center gap-1 text-[10px] text-muted-foreground/60">
            <Clock className="h-2.5 w-2.5" />
            {toolCall.durationMs < 1000
              ? `${Math.round(toolCall.durationMs)}ms`
              : `${(toolCall.durationMs / 1000).toFixed(1)}s`}
          </span>
        )}

        {/* Error preview */}
        {isError && toolCall.error && (
          <span className="flex items-center gap-1 truncate text-[10px] text-red-600/70 dark:text-red-400/70 max-w-[200px]">
            <AlertCircle className="h-2.5 w-2.5 shrink-0" />
            {toolCall.error.slice(0, 60)}
          </span>
        )}

        {/* Spacer */}
        <span className="flex-1" />

        {/* Expand chevron */}
        {expanded ? (
          <ChevronDown className="h-3 w-3 text-muted-foreground/40" />
        ) : (
          <ChevronRight className="h-3 w-3 text-muted-foreground/40" />
        )}
      </button>

      {/* Expandable detail area */}
      {expanded && (
        <div className="border-t border-border/30 px-3 py-2 space-y-2">
          {/* Iteration info */}
          <div className="text-[10px] text-muted-foreground/50">
            第 {toolCall.iteration} 轮　{toolCall.startedAt}
          </div>

          {/* Arguments (collapsible) */}
          {Object.keys(args).length > 0 && (
            <div>
              <button
                onClick={(e) => { e.stopPropagation(); setShowArgs(!showArgs); }}
                className="flex items-center gap-1 text-[10px] font-medium text-muted-foreground/60 hover:text-foreground/70"
              >
                {showArgs ? <ChevronDown className="h-2.5 w-2.5" /> : <ChevronRight className="h-2.5 w-2.5" />}
                请求参数
              </button>
              {showArgs && (
                <pre className="mt-1 max-h-32 overflow-auto rounded-md bg-secondary/40 p-2 text-[10px] text-muted-foreground/70 whitespace-pre-wrap font-mono leading-relaxed">
                  {formatArgs(args)}
                </pre>
              )}
            </div>
          )}

          {/* Result (collapsible, shown when done) */}
          {!isRunning && (
            <div>
              <button
                onClick={(e) => { e.stopPropagation(); setShowResult(!showResult); }}
                className={cn(
                  "flex items-center gap-1 text-[10px] font-medium hover:text-foreground/70",
                  isOk ? "text-emerald-600/70 dark:text-emerald-400/70" : "text-red-600/70 dark:text-red-400/70",
                )}
              >
                {showResult ? <ChevronDown className="h-2.5 w-2.5" /> : <ChevronRight className="h-2.5 w-2.5" />}
                {isOk ? "执行结果" : "错误信息"}
              </button>
              {showResult && (
                <pre
                  className={cn(
                    "mt-1 max-h-48 overflow-auto rounded-md p-2 text-[10px] whitespace-pre-wrap font-mono leading-relaxed",
                    isOk
                      ? "bg-secondary/60 text-foreground/75"
                      : "bg-destructive/10 text-destructive",
                  )}
                >
                  {isOk ? formatResult(toolCall.result) : toolCall.error || "未知错误"}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
});

/** Compact inline tool call indicator for assistant messages that have tool_calls. */
interface InlineToolCallsProps {
  toolCalls: Array<{ id: string; function: { name: string; arguments: string } }>;
}

export const InlineToolCalls = memo(function InlineToolCalls({ toolCalls }: InlineToolCallsProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="my-2 animate-enter-up">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 rounded-xl border border-border/60 bg-card/45 px-3 py-2 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
      >
        <Wrench className="h-3 w-3" />
        <span>调用 {toolCalls.length} 个工具</span>
        {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
      </button>
      {expanded && (
        <div className="mt-1 space-y-0.5 pl-6">
          {toolCalls.map((tc) => {
            const fn = tc.function ?? { name: "未知工具", arguments: "" };
            let argsStr = fn.arguments || "";
            try { argsStr = JSON.stringify(JSON.parse(argsStr), null, 2); } catch {}
            return (
              <div key={tc.id} className="text-[10px] text-muted-foreground/50 font-mono">
                <span className="font-semibold text-foreground/60">{fn.name}</span>
                <span className="ml-1">{argsStr.length > 120 ? argsStr.slice(0, 120) + "…" : argsStr}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
});
