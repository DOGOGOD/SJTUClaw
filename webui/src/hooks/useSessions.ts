import { useState, useEffect, useCallback } from "react";
import { fetchSessions, createSession, deleteSession, renameSession } from "@/lib/api";
import type { SessionSummary } from "@/lib/types";

export function useSessions() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await fetchSessions();
      if (data.ok) {
        setSessions(data.sessions || []);
      }
    } catch (e) {
      console.error("Failed to load sessions", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const create = useCallback(async () => {
    const data = await createSession();
    await refresh();
    return data;
  }, [refresh]);

  const remove = useCallback(
    async (sessionId: string) => {
      await deleteSession(sessionId);
      await refresh();
    },
    [refresh]
  );

  const rename = useCallback(
    async (sessionId: string, title: string) => {
      await renameSession(sessionId, title);
      await refresh();
    },
    [refresh]
  );

  /** Optimistically update a session's title in local state (no server round-trip). */
  const updateTitle = useCallback((sessionId: string, title: string) => {
    setSessions((prev) =>
      prev.map((s) =>
        s.sessionId === sessionId ? { ...s, title } : s
      )
    );
  }, []);

  return { sessions, loading, refresh, createChat: create, deleteChat: remove, renameChat: rename, updateTitle };
}
