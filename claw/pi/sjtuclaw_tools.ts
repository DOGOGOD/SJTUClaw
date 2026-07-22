/** Expose SJTUClaw's Python ToolRegistry to Pi without replacing Pi tools. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { readFileSync } from "node:fs";
import { Type } from "typebox";

interface ManifestTool {
	name: string;
	description: string;
	parameters: Record<string, unknown>;
	safety_level: string;
}

interface ToolManifest {
	version: number;
	tools: ManifestTool[];
}

interface BridgeResponse {
	ok: boolean;
	result: string;
}

function loadManifest(): ToolManifest | undefined {
	const path = process.env.SJTUCLAW_PI_TOOL_MANIFEST;
	if (!path) return undefined;
	try {
		const parsed: unknown = JSON.parse(readFileSync(path, "utf-8"));
		if (!parsed || typeof parsed !== "object") return undefined;
		const candidate = parsed as Partial<ToolManifest>;
		if (candidate.version !== 1 || !Array.isArray(candidate.tools)) return undefined;
		return candidate as ToolManifest;
	} catch {
		return undefined;
	}
}

function oneLine(text: string): string {
	return text.replace(/[\r\n]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 180);
}

export default function sjtuclawTools(pi: ExtensionAPI): void {
	const manifest = loadManifest();
	if (!manifest) return;
	const token = process.env.SJTUCLAW_PI_BRIDGE_TOKEN ?? "";

	for (const tool of manifest.tools) {
		if (!tool.name || !tool.description || !tool.parameters) continue;
		const promptGuidelines: string[] = [];
		if (tool.name === "recall") {
			promptGuidelines.push("Use recall before answering questions about the user's stored preferences, projects, or prior decisions.");
		}
		if (tool.name === "remember") {
			promptGuidelines.push("Use remember for durable user preferences, project facts, and decisions that should survive future sessions.");
		}

		pi.registerTool({
			name: tool.name,
			label: `SJTUClaw: ${tool.name}`,
			description: tool.description,
			promptSnippet: oneLine(tool.description),
			promptGuidelines,
			parameters: Type.Unsafe<Record<string, unknown>>(tool.parameters),
			executionMode: "sequential",
			async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
				const payload = JSON.stringify({ token, toolName: tool.name, input: params });
				const raw = await ctx.ui.input("SJTUClaw 工具桥接", payload);
				if (!raw) {
					return {
						content: [{ type: "text", text: "SJTUClaw 工具桥接未返回结果。" }],
						details: { bridge: true },
						isError: true,
					};
				}
				try {
					const response = JSON.parse(raw) as BridgeResponse;
					return {
						content: [{ type: "text", text: response.result }],
						details: { bridge: true },
						isError: !response.ok,
					};
				} catch {
					return {
						content: [{ type: "text", text: "SJTUClaw 工具桥接返回了无效数据。" }],
						details: { bridge: true },
						isError: true,
					};
				}
			},
		});
	}
}
