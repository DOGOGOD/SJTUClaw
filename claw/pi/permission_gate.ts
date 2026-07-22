/** Route Pi's mutating built-in tools through SJTUClaw's approval UI. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const guardedTools = new Set(["bash", "edit", "write"]);

export default function permissionGate(pi: ExtensionAPI) {
	pi.on("tool_call", async (event, ctx) => {
		if (!guardedTools.has(event.toolName)) return undefined;
		const confirmed = await ctx.ui.confirm(
			"SJTUClaw 工具审批",
			JSON.stringify({ toolName: event.toolName, input: event.input }),
		);
		if (!confirmed) return { block: true, reason: "SJTUClaw user approval was not granted" };
		return undefined;
	});
}
