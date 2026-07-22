/** Bridge SJTUClaw's existing OpenAI-compatible LLM settings into Pi. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function sjtuclawProvider(pi: ExtensionAPI) {
	const baseUrl = process.env.SJTUCLAW_PI_BASE_URL;
	const modelId = process.env.SJTUCLAW_PI_MODEL;
	if (!baseUrl || !modelId) return;

	const contextWindow = Number.parseInt(process.env.SJTUCLAW_PI_CONTEXT_WINDOW ?? "32000", 10);
	const maxTokens = Number.parseInt(process.env.SJTUCLAW_PI_MAX_TOKENS ?? "4096", 10);
	const reasoning = process.env.SJTUCLAW_PI_REASONING === "true";

	pi.registerProvider("sjtuclaw", {
		name: "SJTUClaw OpenAI-compatible",
		baseUrl,
		apiKey: "$SJTUCLAW_PI_API_KEY",
		api: "openai-completions",
		models: [
			{
				id: modelId,
				name: modelId,
				reasoning,
				input: ["text", "image"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: Number.isFinite(contextWindow) ? contextWindow : 32000,
				maxTokens: Number.isFinite(maxTokens) ? maxTokens : 4096,
			},
		],
	});
}
