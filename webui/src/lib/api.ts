const API_BASE = "";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60_000);
  try {
    const res = await fetch(`${API_BASE}${url}`, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...options,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(text || `HTTP ${res.status}`);
    }
    return res.json();
  } finally {
    clearTimeout(timeout);
  }
}

// Sessions
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

// Messages
export async function fetchMessages(sessionId: string): Promise<{ ok: boolean; messages: import("@/lib/types").ChatMessage[] }> {
  return request(`/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function sendMessage(data: import("@/lib/types").SendMessageRequest): Promise<import("@/lib/types").SendMessageResponse> {
  return request("/chat", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function sendCommand(data: { sessionId: string; command: string }): Promise<{ ok: boolean; result: string }> {
  return request("/command", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

// Workspace
export async function fetchWorkspace(sessionId: string): Promise<import("@/lib/types").WorkspaceInfo> {
  return request(`/workspace?sessionId=${encodeURIComponent(sessionId)}`);
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

// System Prompt
export async function fetchSystemPrompt(): Promise<import("@/lib/types").SystemPromptPayload> {
  return request("/admin/system-prompt");
}

export async function saveSystemPrompt(content: string): Promise<{ ok: boolean }> {
  return request("/admin/system-prompt", {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

// Soul
export async function fetchSoul(): Promise<import("@/lib/types").SoulPayload> {
  return request("/admin/soul");
}

export async function saveSoul(content: string): Promise<{ ok: boolean }> {
  return request("/admin/soul", {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

// Memories
export async function fetchMemories(): Promise<{ ok: boolean; memories: import("@/lib/types").MemoryEntry[] }> {
  return request("/memories");
}

export async function addMemory(content: string): Promise<{ ok: boolean }> {
  return request("/memories", {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}

export async function deleteMemory(id: string): Promise<{ ok: boolean }> {
  return request(`/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// Tasks
export async function fetchTasks(): Promise<{ ok: boolean; tasks: import("@/lib/types").TaskInfo[] }> {
  return request("/tasks");
}

export async function createTask(data: {
  content: string;
  triggerType: string;
  triggerRule: string;
  sessionId: string;
}): Promise<{ ok: boolean }> {
  return request("/tasks", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function cancelTask(id: string): Promise<{ ok: boolean }> {
  return request(`/tasks/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export async function deleteTask(id: string): Promise<{ ok: boolean }> {
  return request(`/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// Approvals
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

// Skills
export async function fetchSkills(): Promise<{ ok: boolean; skills: import("@/lib/types").SkillInfo[] }> {
  return request("/skills");
}

export async function fetchSkillDetail(name: string): Promise<{ ok: boolean; skill: { name: string; description: string; instructions: string } }> {
  return request(`/skills/${encodeURIComponent(name)}`);
}

// Attachments
export async function uploadAttachment(sessionId: string, file: File): Promise<{ ok: boolean }> {
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
      const text = await res.text().catch(() => "");
      throw new Error(text || `Upload failed: HTTP ${res.status}`);
    }
    return res.json();
  } finally {
    clearTimeout(timeout);
  }
}
