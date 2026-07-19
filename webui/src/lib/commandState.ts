import type { ChatMessage } from "@/lib/types";

export type CommandNavigation =
  | { kind: "switch"; sessionId: string }
  | { kind: "clear" }
  | null;

export function resolveCommandNavigation(
  actions: string[] | undefined,
  switchToSessionId: string | undefined,
): CommandNavigation {
  if (actions?.includes("switch_session") && switchToSessionId) {
    return { kind: "switch", sessionId: switchToSessionId };
  }
  if (actions?.includes("clear_session")) return { kind: "clear" };
  return null;
}

export function messagesAfterCommandRefresh(
  persisted: ChatMessage[],
  userCommand: ChatMessage,
  commandResult: ChatMessage | null,
): ChatMessage[] {
  return commandResult
    ? [...persisted, userCommand, commandResult]
    : [...persisted, userCommand];
}
