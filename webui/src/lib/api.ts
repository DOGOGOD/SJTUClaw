const API_BASE = "";

// Deduplication map: prevents concurrent duplicate GET requests
const _pendingRequests = new Map<string, Promise<any>>();

/** Safely read a Response body as text without consuming it twice. */
async function _readResponseText(res: Response): Promise<string> {
  try {
    return await res.text();
  } catch {
    return "";
  }
}

/** Parse a JSON response, returning a clear error for HTML fallbacks. */
async function _parseJsonResponse<T>(res: Response): Promise<T> {
  const text = await _readResponseText(res);
  if (!text || !text.trim()) {
    throw new Error(`服务器返回空响应 (HTTP ${res.status})`);
  }
  const isHtml = text.trimStart().toLowerCase().startsWith("<!doctype") || text.trimStart().startsWith("<");
  if (isHtml) {
    throw new Error(`服务器返回了网页而不是 JSON，请确认后端服务已启动 (HTTP ${res.status})`);
  }
  try {
    return JSON.parse(text) as T;
  } catch (parseErr) {
    const snippet = text.length > 120 ? `${text.slice(0, 120)}…` : text;
    throw new Error(`响应解析失败: ${(parseErr as Error).message}，内容: ${snippet}`);
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const method = options?.method || "GET";
  const cacheKey = method === "GET" ? `${method}:${url}` : "";

  // Deduplicate concurrent GET requests
  if (cacheKey && _pendingRequests.has(cacheKey)) {
    return _pendingRequests.get(cacheKey)!;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60_000);

  const promise = (async () => {
    try {
      const res = await fetch(`${API_BASE}${url}`, {
        headers: options?.body instanceof FormData
          ? undefined
          : { "Content-Type": "application/json" },
        signal: controller.signal,
        ...options,
      });
      if (!res.ok) {
        const text = await _readResponseText(res);
        let message = text || `请求失败: HTTP ${res.status}`;
        try {
          const parsed = JSON.parse(text) as { detail?: string; error?: string };
          message = parsed.detail || parsed.error || message;
        } catch {}
        throw new Error(message);
      }
      return _parseJsonResponse<T>(res);
    } catch (err) {
      // Surface network/abort errors with friendly text
      if ((err as Error).name === "AbortError") {
        throw new Error("请求超时，请稍后重试");
      }
      throw err;
    } finally {
      clearTimeout(timeout);
      if (cacheKey) _pendingRequests.delete(cacheKey);
    }
  })();

  if (cacheKey) _pendingRequests.set(cacheKey, promise);
  return promise;
}

// ---------------------------------------------------------------------------

export async function fetchSessions(): Promise<{ ok: boolean; sessions: import("@/lib/types").SessionSummary[] }> {
  return request("/sessions");
}

export async function createSession(): Promise<{ ok: boolean; sessionId: string; title: string }> {
  return request("/sessions", { method: "POST", body: "{}" });
}

export async function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  return request(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export async function renameSession(sessionId: string, title: string): Promise<{ ok: boolean }> {
  return request(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

// ---------------------------------------------------------------------------

export async function fetchMessages(sessionId: string): Promise<{
  ok: boolean; sessionId: string; messages: import("@/lib/types").ChatMessage[]; summary: string;
  autoMode?: boolean; unlimitedMode?: boolean;
}> {
  return request(`/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function sendMessage(data: import("@/lib/types").SendMessageRequest): Promise<import("@/lib/types").SendMessageResponse> {
  return request("/chat", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

/**
 * Cancel a running agent turn.
 *
 * If sessionId is provided, cancels only that session's turn.
 * If all=true or sessionId is omitted, cancels all active turns.
 */
export async function stopChat(data: { sessionId?: string; all?: boolean }): Promise<{
  ok: boolean;
  cancelled: number;
  message: string;
}> {
  return request("/stop", {
    method: "POST",
    body: JSON.stringify({ session_id: data.sessionId, all: data.all ?? false }),
  });
}

/**
 * Stream agent turn events via SSE.
 *
 * Returns an AbortController that the caller can use to cancel the stream,
 * and calls `onEvent` for each parsed SSE event.
 */
export function streamChat(
  data: { sessionId: string; message: string },
  onEvent: (event: import("@/lib/types").SSEEvent) => void,
  onError?: (error: Error) => void,
  onDone?: () => void,
): AbortController {
  const controller = new AbortController();

  fetch(`${API_BASE}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId: data.sessionId, message: data.message }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(text || `HTTP ${response.status}`);
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error("Stream not supported");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        // Keep the last partial line in the buffer
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const jsonStr = line.slice(6);
            if (jsonStr === ": keepalive") continue;
            try {
              const parsed = JSON.parse(jsonStr);
              onEvent(parsed as import("@/lib/types").SSEEvent);
            } catch {
              // Skip unparseable lines
            }
          }
          // Ignore comment lines (starting with ":")
        }
      }
    })
    .then(() => onDone?.())
    .catch((err) => {
      if ((err as Error).name !== "AbortError") {
        onError?.(err as Error);
      }
      onDone?.();
    });

  return controller;
}

export async function sendCommand(data: { sessionId: string; command: string }): Promise<{
  ok: boolean;
  type: string;
  result: string;
  format?: "markdown" | "plain";
  actions?: string[];
  switchToSessionId?: string;
  autoMode?: boolean;
  unlimitedMode?: boolean;
}> {
  return request("/command", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// ---------------------------------------------------------------------------

export async function fetchWorkspace(sessionId: string): Promise<import("@/lib/types").WorkspaceInfo> {
  return request(`/workspace?sessionId=${encodeURIComponent(sessionId)}`);
}

export async function pickWorkspace(): Promise<{ ok: boolean; cancelled?: boolean; path: string }> {
  return request("/workspace/pick", { method: "POST", body: "{}" });
}

export async function setWorkspace(sessionId: string, path: string): Promise<{ ok: boolean; workspace: string }> {
  return request("/workspace", {
    method: "POST",
    body: JSON.stringify({ sessionId, path }),
  });
}

export async function unsetWorkspace(sessionId: string): Promise<{ ok: boolean }> {
  return request(`/workspace?sessionId=${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------

export async function fetchSystemPrompt(): Promise<import("@/lib/types").SystemPromptPayload> {
  return request("/admin/system-prompt");
}

export async function saveSystemPrompt(content: string): Promise<{ ok: boolean }> {
  return request("/admin/system-prompt", {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

// ---------------------------------------------------------------------------

export async function fetchSoul(): Promise<import("@/lib/types").SoulPayload> {
  return request("/admin/soul");
}

export async function saveSoul(content: string): Promise<{ ok: boolean }> {
  return request("/admin/soul", {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

// ---------------------------------------------------------------------------

export async function fetchLLMSettings(): Promise<{ ok: boolean; settings: import("@/lib/types").LLMSettings }> {
  return request("/settings/llm");
}

export async function saveLLMSettings(data: {
  baseUrl: string;
  apiKey?: string;
  model: string;
  contextWindow: number;
  contextUsageRatio: number;
  maxOutputTokens: number;
  consolidationRatio: number;
}): Promise<{ ok: boolean; settings: import("@/lib/types").LLMSettings }> {
  return request("/settings/llm", {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function fetchChannelSettings(): Promise<{
  ok: boolean;
  settings: { qq: import("@/lib/types").QQChannelSettings };
  status: import("@/lib/types").QQConnectionStatus;
}> {
  return request("/settings/channel");
}

export async function saveQQChannelSettings(data: {
  enabled: boolean;
  appId: string;
  clientSecret?: string;
  allowFrom: string;
  msgFormat: "markdown" | "text";
  ackMessage: string;
}): Promise<{
  ok: boolean;
  settings: { qq: import("@/lib/types").QQChannelSettings };
  status: import("@/lib/types").QQConnectionStatus;
}> {
  return request("/settings/channel/qq", {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function startQQOnboard(): Promise<{
  ok: boolean;
  taskId: string;
  connectUrl: string;
  qrImage: string;
}> {
  return request("/settings/channel/qq/onboard/start", { method: "POST" });
}

export async function pollQQOnboard(taskId: string): Promise<{
  ok: boolean;
  status: "pending" | "completed" | "expired";
  message?: string;
  settings?: { qq: import("@/lib/types").QQChannelSettings };
  connection?: import("@/lib/types").QQConnectionStatus;
}> {
  return request(`/settings/channel/qq/onboard/${encodeURIComponent(taskId)}`);
}

// ---------------------------------------------------------------------------

export async function fetchMemories(): Promise<{ ok: boolean; memories: import("@/lib/types").MemoryEntry[] }> {
  return request("/memories");
}

export async function addMemory(data: {
  content: string;
  category?: string;
  tags?: string[];
  importance?: number;
}): Promise<{ ok: boolean; id: string }> {
  return request("/memories", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function deleteMemory(id: string): Promise<{ ok: boolean }> {
  return request(`/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------

export async function fetchCronJobs(): Promise<{ ok: boolean; jobs: import("@/lib/types").CronJobInfo[] }> {
  return request("/cron/jobs");
}

export async function createCronJob(data: {
  name?: string;
  message: string;
  everySeconds?: number;
  cronExpr?: string;
  tz?: string;
  at?: string;
  sessionId?: string;
}): Promise<{ ok: boolean; job: import("@/lib/types").CronJobInfo }> {
  return request("/cron/jobs", {
    method: "POST",
    body: JSON.stringify({
      name: data.name,
      message: data.message,
      everySeconds: data.everySeconds,
      cronExpr: data.cronExpr,
      tz: data.tz,
      at: data.at,
      sessionId: data.sessionId,
    }),
  });
}

export async function deleteCronJob(id: string): Promise<{ ok: boolean }> {
  return request(`/cron/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function disableCronJob(id: string): Promise<{ ok: boolean; job: import("@/lib/types").CronJobInfo }> {
  return request(`/cron/jobs/${encodeURIComponent(id)}/disable`, { method: "POST" });
}

export async function enableCronJob(id: string): Promise<{ ok: boolean; job: import("@/lib/types").CronJobInfo }> {
  return request(`/cron/jobs/${encodeURIComponent(id)}/enable`, { method: "POST" });
}

// ---------------------------------------------------------------------------

export async function fetchApprovals(sessionId?: string): Promise<{ ok: boolean; approvals: import("@/lib/types").ApprovalInfo[] }> {
  const query = sessionId ? `?sessionId=${encodeURIComponent(sessionId)}` : "";
  return request(`/approvals${query}`);
}

export async function approveApproval(id: string): Promise<{ ok: boolean }> {
  return request(`/approvals/${encodeURIComponent(id)}/approve`, { method: "POST" });
}

export async function rejectApproval(id: string, reason?: string): Promise<{ ok: boolean }> {
  return request(`/approvals/${encodeURIComponent(id)}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// ---------------------------------------------------------------------------

export async function fetchSkills(): Promise<{ ok: boolean; skills: import("@/lib/types").SkillInfo[] }> {
  return request("/skills");
}

export async function uploadSkillPackage(file: File, replace = false): Promise<{
  ok: boolean;
  skill: { name: string; description: string; fileCount: number; path: string };
  message: string;
}> {
  const form = new FormData();
  form.append("file", file);
  return request(`/skills/upload?replace=${replace ? "true" : "false"}`, {
    method: "POST",
    body: form,
  });
}

export async function removeSkill(name: string): Promise<{
  ok: boolean;
  removed: { name: string; message: string };
  message: string;
}> {
  return request(`/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Desktop pet

export async function fetchPetSettings(): Promise<{
  ok: boolean;
  settings: import("@/lib/types").PetSettings;
  running: boolean;
}> {
  return request("/pet/settings");
}

export async function savePetSettings(data: Partial<{
  enabled: boolean;
  selectedPetId: string;
  launchOnGatewayStart: boolean;
}>): Promise<{
  ok: boolean;
  settings: import("@/lib/types").PetSettings;
  running: boolean;
}> {
  return request("/pet/settings", {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function fetchPets(): Promise<{
  ok: boolean;
  pets: import("@/lib/types").PetInfo[];
}> {
  return request("/pet/pets");
}

export async function openPet(): Promise<{
  ok: boolean;
  settings: import("@/lib/types").PetSettings;
  running: boolean;
}> {
  return request("/pet/open", { method: "POST" });
}

export async function closePet(): Promise<{
  ok: boolean;
  settings: import("@/lib/types").PetSettings;
  running: boolean;
}> {
  return request("/pet/close", { method: "POST" });
}

export async function uploadPet(data: {
  petId: string;
  displayName: string;
  description: string;
  spritesheet: File;
}): Promise<{ ok: boolean; pet: import("@/lib/types").PetInfo }> {
  const form = new FormData();
  form.append("petId", data.petId);
  form.append("displayName", data.displayName);
  form.append("description", data.description);
  form.append("spritesheet", data.spritesheet);
  return request("/pet/pets", { method: "POST", body: form });
}

export async function deletePet(petId: string): Promise<{ ok: boolean }> {
  return request(`/pet/pets/${encodeURIComponent(petId)}`, { method: "DELETE" });
}

export async function fetchSkillDetail(name: string): Promise<{
  ok: boolean;
  skill: { name: string; description: string; instructions: string; assets?: string[]; references?: string[] };
}> {
  return request(`/skills/${encodeURIComponent(name)}`);
}

// ---------------------------------------------------------------------------

export async function uploadAttachment(sessionId: string, file: File): Promise<{
  ok: boolean;
  attachment?: { id: string; originalName: string; storedName: string; size: number; mimeType: string; uploadedAt: string };
  message?: { role: "user"; content: string; command?: boolean };
}> {
  const fd = new FormData();
  fd.append("file", file);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120_000);
  try {
    const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/attachments`, {
      method: "POST",
      body: fd,
      signal: controller.signal,
    });
    if (!res.ok) {
      const text = await _readResponseText(res);
      throw new Error(text || `上传失败: HTTP ${res.status}`);
    }
    return _parseJsonResponse(res);
  } finally {
    clearTimeout(timeout);
  }
}
