export interface SessionSummary {
  sessionId: string;
  title: string;
  messageCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface ChatMessage {
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  tool_calls?: ToolCallData[];
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
  createdAt: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  hasAssets: boolean;
  hasReferences: boolean;
}

export interface TaskInfo {
  id: string;
  content: string;
  triggerType: "once" | "interval" | "daily";
  triggerRule: string;
  status: "waiting" | "running" | "completed" | "cancelled" | "failed";
  nextRunAt: string | null;
  executionHistory: TaskExecution[];
  sessionId: string;
}

export interface TaskExecution {
  executedAt: string;
  status: "success" | "error";
  reply?: string;
  error?: string;
}

export interface ApprovalInfo {
  approvalId: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  sessionId: string;
  createdAt: string;
}

export interface SystemPromptPayload {
  content: string;
}

export interface SoulPayload {
  content: string;
}

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
}

export type SettingsSection = "prompt" | "soul" | "memory" | "sessions" | "tasks" | "workspace" | "approval" | "skills";

export type ShellView = "chat" | "settings";
