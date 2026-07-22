import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchSessions, sendMessage, uploadPet } from "./api";

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
    let requestSignal: AbortSignal | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      requestSignal = init?.signal ?? null;
      return new Promise((resolve) => {
        setTimeout(() => resolve(jsonResponse({ ok: true, messages: [] })), 61_000);
      });
    });

    const request = sendMessage({ sessionId: "session-a", message: "long task" });

    await vi.advanceTimersByTimeAsync(60_000);
    expect(requestSignal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1_000);
    await expect(request).resolves.toMatchObject({ ok: true });
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
