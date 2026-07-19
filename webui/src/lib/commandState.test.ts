import { describe, expect, it } from "vitest";
import { messagesAfterCommandRefresh, resolveCommandNavigation } from "./commandState";

describe("slash command state", () => {
  it("keeps the ephemeral command and result after persisted messages reload", () => {
    const persisted = [{ role: "assistant" as const, content: "older" }];
    const command = { role: "user" as const, content: "/rollback" };
    const result = { role: "assistant" as const, content: "回退完成", command: true };

    expect(messagesAfterCommandRefresh(persisted, command, result)).toEqual([
      ...persisted,
      command,
      result,
    ]);
  });

  it("resolves switch and clear actions returned by the gateway", () => {
    expect(resolveCommandNavigation(["switch_session"], "session_002")).toEqual({
      kind: "switch",
      sessionId: "session_002",
    });
    expect(resolveCommandNavigation(["clear_session"], undefined)).toEqual({ kind: "clear" });
  });
});
