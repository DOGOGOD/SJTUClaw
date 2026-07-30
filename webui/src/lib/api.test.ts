import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createSession,
  fetchSessions,
  sendMessage,
  streamChat,
  uploadPet,
} from "./api";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: vi.fn().mockResolvedValue(JSON.stringify(body)),
  } as unknown as Response;
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("API request timeouts", () => {
  it("keeps the 60 second timeout for ordinary requests", async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => (
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      })
    ));

    const request = fetchSessions();
    const rejection = expect(request).rejects.toThrow("请求超时，请稍后重试");

    await vi.advanceTimersByTimeAsync(60_000);
    await rejection;
  });

  it("does not abort a long-running chat turn after 60 seconds", async () => {
    vi.useFakeTimers();
    const requestState: { signal: AbortSignal | null } = { signal: null };
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      requestState.signal = init?.signal ?? null;
      return new Promise((resolve) => {
        setTimeout(() => resolve(jsonResponse({ ok: true, messages: [] })), 61_000);
      });
    });

    const request = sendMessage({ sessionId: "session-a", message: "long task" });

    await vi.advanceTimersByTimeAsync(60_000);
    expect(requestState.signal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1_000);
    await expect(request).resolves.toMatchObject({ ok: true });
  });
});

describe("API request freshness", () => {
  it("does not reuse an older pending GET after a mutation", async () => {
    let resolveFirstGet: ((response: Response) => void) | undefined;
    let getCount = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      (_input, init) => {
        if (init?.method === "POST") {
          return Promise.resolve(
            jsonResponse({ ok: true, sessionId: "new-session", title: "新会话" }),
          );
        }
        getCount += 1;
        if (getCount === 1) {
          return new Promise<Response>((resolve) => {
            resolveFirstGet = resolve;
          });
        }
        return Promise.resolve(
          jsonResponse({
            ok: true,
            sessions: [{ sessionId: "new-session", title: "新会话" }],
          }),
        );
      },
    );

    const older = fetchSessions();
    await createSession();
    const fresh = fetchSessions();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    await expect(fresh).resolves.toMatchObject({
      sessions: [{ sessionId: "new-session" }],
    });

    resolveFirstGet?.(jsonResponse({ ok: true, sessions: [] }));
    await older;
  });

  it("flushes a final split UTF-8 SSE event without a trailing newline", async () => {
    const payload = 'data: {"type":"final","content":"猫"}';
    const bytes = new TextEncoder().encode(payload);
    const splitAt = bytes.indexOf(0xe7) + 1;
    const reader = {
      read: vi.fn()
        .mockResolvedValueOnce({ done: false, value: bytes.slice(0, splitAt) })
        .mockResolvedValueOnce({ done: false, value: bytes.slice(splitAt) })
        .mockResolvedValueOnce({ done: true, value: undefined }),
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      body: { getReader: () => reader },
    } as unknown as Response);
    const events: Array<{ type: string; content: string }> = [];

    await new Promise<void>((resolve, reject) => {
      streamChat(
        { sessionId: "session-a", message: "hi" },
        (event) => events.push(event as { type: string; content: string }),
        reject,
        resolve,
      );
    });

    expect(events).toEqual([{ type: "final", content: "猫" }]);
  });
});

describe("pet package upload", () => {
  it("sends the ZIP file as the package form field", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({
        ok: true,
        pet: { id: "coding-cat" },
        replyGeneration: { source: "llm", count: 12, warning: "" },
      }),
    );
    const packageFile = new File(["zip"], "coding-cat.zip", { type: "application/zip" });

    await uploadPet(packageFile);

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.method).toBe("POST");
    const body = init?.body as FormData;
    expect(body.get("package")).toBe(packageFile);
    expect(body.get("spritesheet")).toBeNull();
  });
});
