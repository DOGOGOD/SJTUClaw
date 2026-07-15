export interface SessionSummary {
  sessionId: string;
  title: string;
  messageCount: number;
  updatedAt: string;
}

export interface ChatMessage {
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  format?: "markdown" | "plain";
  command?: boolean;
  tool_calls?: ToolCallData[];
  tool_call_id?: string;
  name?: string;
  timestamp?: string;
}

export interface ToolCallData {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string;
  };
}

export interface WorkspaceInfo {
  ok: boolean;
  sessionId: string;
  workspace: string | null;
  isSet: boolean;
}

export interface MemoryEntry {
  id: string;
  content: string;
  category: string;
  tags: string[];
  importance: number;
  sourceSessionId: string;
  createdAt: string;
  updatedAt: string;
  lastRecalledAt: string | null;
  recallCount: number;
}

export interface SkillInfo {
  name: string;
  description: string;
  hasAssets: boolean;
  hasReferences: boolean;
}

// ── Cron Job types ──────────────────────────────────────────────────────

export interface CronJobSchedule {
  kind: "at" | "every" | "cron";
  atMs: number | null;
  everyMs: number | null;
  expr: string | null;
  tz: string | null;
}

export interface CronJobPayload {
  kind: "system_event" | "agent_turn";
  message: string;
  deliver: boolean;
  channel: string | null;
  to: string | null;
  channelMeta: Record<string, unknown>;
  sessionKey: string | null;
  originChannel: string | null;
  originChatId: string | null;
}

export interface CronJobRunRecord {
  runAtMs: number;
  status: "ok" | "error" | "skipped";
  durationMs: number;
  error: string | null;
}

export interface CronJobInfo {
  id: string;
  name: string;
  enabled: boolean;
  schedule: CronJobSchedule;
  payload: CronJobPayload;
  state: {
    nextRunAtMs: number | null;
    lastRunAtMs: number | null;
    lastStatus: "ok" | "error" | "skipped" | null;
    lastError: string | null;
    runHistory: CronJobRunRecord[];
  };
  createdAtMs: number;
  updatedAtMs: number;
  deleteAfterRun: boolean;
}

// ── Approval ─────────────────────────────────────────────────────────────

export interface ApprovalInfo {
  approvalId: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  sessionId: string;
  createdAt: string;
}

// ── System content ──────────────────────────────────────────────────────

export interface SystemPromptPayload {
  content: string;
}

export interface SoulPayload {
  content: string;
}

// ── Chat request / response ─────────────────────────────────────────────

export interface SendMessageRequest {
  sessionId: string;
  message: string;
}

export interface SendMessageResponse {
  ok: boolean;
  type: string;
  sessionId: string;
  reply: string;
  messages?: ChatMessage[];
  error?: string;
  autoMode?: boolean;
  unlimitedMode?: boolean;
  /** Auto-generated session title from the first user message (null if not applicable). */
  title?: string | null;
}

// ── SSE Streaming events ─────────────────────────────────────────────────

export type SSEEventType =
  | "ThinkingEvent"
  | "ToolCallStartEvent"
  | "ToolCallEndEvent"
  | "FinalEvent"
  | "ErrorEvent"
  | "_session_info"
  | "_title"
  | "_done";

export interface SSEThinkingEvent {
  type: "ThinkingEvent";
  iteration: number;
  timestamp: string;
}

export interface SSEToolCallStartEvent {
  type: "ToolCallStartEvent";
  call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  iteration: number;
  timestamp: string;
}

export interface SSEToolCallEndEvent {
  type: "ToolCallEndEvent";
  call_id: string;
  tool_name: string;
  ok: boolean;
  result: string | null;
  error: string | null;
  duration_ms: number;
  timestamp: string;
}

export interface SSEFinalEvent {
  type: "FinalEvent";
  content: string;
  timestamp: string;
}

export interface SSEErrorEvent {
  type: "ErrorEvent";
  error: string;
  timestamp: string;
}

export interface SSESessionInfoEvent {
  type: "_session_info";
  sessionId: string;
  autoMode: boolean;
}

export interface SSETitleEvent {
  type: "_title";
  title: string;
}

export interface SSEDoneEvent {
  type: "_done";
}

export type SSEEvent =
  | SSEThinkingEvent
  | SSEToolCallStartEvent
  | SSEToolCallEndEvent
  | SSEFinalEvent
  | SSEErrorEvent
  | SSESessionInfoEvent
  | SSETitleEvent
  | SSEDoneEvent;

/** Live tool call state tracked during streaming. */
export interface LiveToolCall {
  callId: string;
  toolName: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error";
  result: string | null;
  error: string | null;
  durationMs: number | null;
  startedAt: string;
  iteration: number;
}

// ── Navigation ───────────────────────────────────────────────────────────

export interface PetSettings {
  enabled: boolean;
  selectedPetId: string;
  launchOnGatewayStart: boolean;
  position: { x: number | null; y: number | null };
}

export interface PetInfo {
  id: string;
  displayName: string;
  description: string;
  spriteVersionNumber: 1 | 2;
  spritesheetUrl: string;
  source: "bundled" | "user";
  readOnly: boolean;
}

export interface LLMSettings {
  baseUrl: string;
  apiKeyMasked: string;
  apiKeyConfigured: boolean;
  model: string;
  contextWindow: number;
  contextUsageRatio: number;
  maxOutputTokens: number;
  consolidationRatio: number;
}

export interface QQChannelSettings {
  enabled: boolean;
  appId: string;
  clientSecretMasked: string;
  allowFrom: string;
  msgFormat: "markdown" | "text";
  ackMessage: string;
}

export interface QQConnectionStatus {
  enabled: boolean;
  configured: boolean;
  running: boolean;
  starting?: boolean;
  appId: string;
  message: string;
}

export type SettingsSection = "prompt" | "soul" | "memory" | "cron" | "skills" | "workspace" | "pet" | "channel" | "llm";

export type ShellView = "chat" | "settings";
