import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Sidebar } from "@/components/sidebar/Sidebar";
import { ThreadShell } from "@/components/thread/ThreadShell";
import { SettingsView } from "@/components/settings/SettingsView";
import { ThemeProvider, useTheme } from "@/hooks/useTheme";
import { useSessions } from "@/hooks/useSessions";
import { cn } from "@/lib/utils";
import { isSlashCommand } from "@/lib/commands";
import { fetchMessages, sendMessage, sendCommand, stopChat, uploadAttachment, renameSession, fetchApprovals, approveApproval, rejectApproval } from "@/lib/api";
import type { ApprovalInfo } from "@/lib/types";
import type { ChatMessage, SettingsSection, ShellView } from "@/lib/types";

const SIDEBAR_WIDTH = 288;
const SIDEBAR_COLLAPSED_WIDTH = 0;

function Shell() {
  const { theme, toggle: toggleTheme } = useTheme();
  const { sessions, loading: sessionsLoading, refresh: refreshSessions, createChat, deleteChat, updateTitle } = useSessions();

  const [view, setView] = useState<ShellView>("chat");
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("prompt");

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [sending, setSending] = useState(false);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<ApprovalInfo | null>(null);
  const [autoMode, setAutoMode] = useState(false);
  const [unlimitedMode, setUnlimitedMode] = useState(false);
  const freshlyCreatedSessionRef = useRef<string | null>(null);

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  useEffect(() => {
    if (!activeSessionId) {
      setMessages([]);
      setMessagesLoading(false);
      // AUTO and UNLIMITED are session-scoped.  Reset stale badges when the
      // user opens a new-chat draft; the new session starts with both off.
      setAutoMode(false);
      setUnlimitedMode(false);
      return;
    }
    if (freshlyCreatedSessionRef.current === activeSessionId) {
      freshlyCreatedSessionRef.current = null;
      setMessagesLoading(false);
      return;
    }
    let cancelled = false;
    setMessagesLoading(true);
    fetchMessages(activeSessionId)
      .then((d) => {
        if (cancelled) return;
        if (d.ok) {
          setMessages(d.messages || []);
          if (d.autoMode !== undefined) setAutoMode(!!d.autoMode);
          if (d.unlimitedMode !== undefined) setUnlimitedMode(!!d.unlimitedMode);
        } else {
          setMessages([]);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.error("Failed to load messages", e);
        setMessages([]);
      })
      .finally(() => { if (!cancelled) setMessagesLoading(false); });
    return () => { cancelled = true; };
  }, [activeSessionId]);

  // 后台轮询：当不在发送消息时，定期检查当前会话是否有新消息
  // 用于感知定时任务（cron）到点后由后端写入 session 的新消息
  useEffect(() => {
    if (!activeSessionId || sending) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      if (cancelled) return;
      try {
        const d = await fetchMessages(activeSessionId);
        if (cancelled) return;
        if (d.ok && d.messages) {
          setMessages((prev) => {
            // 仅在消息数量增加时更新，避免覆盖正在编辑或流式中的状态
            if (d.messages.length > prev.length) {
              return d.messages;
            }
            return prev;
          });
        }
      } catch {}
    }, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [activeSessionId, sending]);

  // 定期刷新会话列表：让侧边栏感知其他会话由定时任务产生的更新（updatedAt / messageCount）
  useEffect(() => {
    const timer = setInterval(() => {
      refreshSessions();
    }, 10000);
    return () => clearInterval(timer);
  }, [refreshSessions]);

  const navigateToChat = useCallback((sessionId: string | null) => {
    setView("chat");
    setActiveSessionId(sessionId);
    setMobileSidebarOpen(false);
  }, []);

  const navigateToSettings = useCallback((section: SettingsSection = "prompt") => {
    setView("settings");
    setSettingsSection(section);
    setMobileSidebarOpen(false);
  }, []);

  // Esc 键关闭设置浮窗
  useEffect(() => {
    if (view !== "settings") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") navigateToChat(activeSessionId);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, activeSessionId, navigateToChat]);

  const handleNewChat = useCallback(() => {
    if (sending) return;
    navigateToChat(null);
  }, [navigateToChat, sending]);

  const handleSelectSession = useCallback((sessionId: string) => {
    if (sending) return;
    navigateToChat(sessionId);
  }, [navigateToChat, sending]);

  const handleDeleteSession = useCallback(async (sessionId: string) => {
    await deleteChat(sessionId);
    if (activeSessionId === sessionId) navigateToChat(null);
  }, [deleteChat, activeSessionId, navigateToChat]);

  const handleRenameSession = useCallback(async (sessionId: string) => {
    const title = prompt("请输入新标题");
    if (!title?.trim()) return;
    try { await renameSession(sessionId, title.trim()); await refreshSessions(); } catch {}
  }, [refreshSessions]);

  const handleSend = useCallback(async (message: string) => {
    setSending(true);
    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const created = await createChat();
        if (!created.sessionId) {
          setSending(false);
          return;
        }
        sessionId = created.sessionId;
        freshlyCreatedSessionRef.current = sessionId;
        navigateToChat(sessionId);
      } catch (e) {
        console.error("Failed to create chat", e);
        setSending(false);
        return;
      }
    }
    const userMsg: ChatMessage = { role: "user", content: message };
    setMessages((prev) => [...prev, userMsg]);

    if (isSlashCommand(message)) {
      try {
        const d = await sendCommand({ sessionId, command: message });
        if (d.ok && d.result) {
          setMessages((prev) => [...prev, {
            role: "assistant",
            content: d.result,
            format: d.format ?? "markdown",
            command: true,
          }]);
          if (d.autoMode !== undefined) setAutoMode(!!d.autoMode);
          if (d.unlimitedMode !== undefined) setUnlimitedMode(!!d.unlimitedMode);
        }
        if (d.actions?.includes("open_pet_settings")) {
          navigateToSettings("pet");
        }
        await refreshSessions();
      } catch (e) {
        console.error("Command failed", e);
        setMessages((prev) => prev.slice(0, -1));
      } finally {
        setSending(false);
      }
      return;
    }

    // ── Polling-based real-time tool call display ──────────────────────
    // Strategy: while POST /chat blocks on the agent turn, we poll
    // GET /messages every second.  The agent loop saves intermediate
    // tool calls to the session, which are visible through the shared
    // in-memory SessionStore cache.

    // Track last message count to avoid redundant state updates
    let lastMsgCount = 0;

    // Approval polling
    const approvalTimer = setInterval(async () => {
      try {
        const ad = await fetchApprovals(sessionId);
        if (ad.approvals?.length > 0) setPendingApproval(ad.approvals[0]);
      } catch {}
    }, 2000);

    // Message polling shows real-time tool call progress (reduced to 2s)
    const msgTimer = setInterval(async () => {
      try {
        const d = await fetchMessages(sessionId);
        if (d.ok && d.messages && d.messages.length !== lastMsgCount) {
          lastMsgCount = d.messages.length;
          setMessages(d.messages);
        }
      } catch {}
    }, 2000);

    try {
      const d = await sendMessage({ sessionId, message });
      if (d.ok) {
        // Use full messages array from response (includes all tool calls)
        if (d.messages && d.messages.length > 0) {
          setMessages(d.messages);
        }
        if ((d as any).autoMode !== undefined) setAutoMode(!!(d as any).autoMode);
        if ((d as any).unlimitedMode !== undefined) setUnlimitedMode(!!(d as any).unlimitedMode);
        if (d.title) updateTitle(sessionId, d.title);
      } else {
        // On failure, restore and re-fetch
        setMessages((prev) => prev.slice(0, -1));
      }
      await refreshSessions();
    } catch (e) {
      console.error("Send failed", e);
      setMessages((prev) => prev.slice(0, -1));
      // Re-fetch to get any partial results
      try {
        const d = await fetchMessages(sessionId);
        if (d.ok) setMessages(d.messages || []);
      } catch {}
    } finally {
      clearInterval(approvalTimer);
      clearInterval(msgTimer);
      setPendingApproval(null);
      setSending(false);
    }
  }, [activeSessionId, createChat, navigateToChat, refreshSessions, updateTitle]);

  const handleStop = useCallback(async () => {
    if (!activeSessionId) return;
    try {
      // Send stop request to cancel the running agent turn
      await stopChat({ sessionId: activeSessionId });
      // Re-fetch messages to show the cancelled state
      try {
        const d = await fetchMessages(activeSessionId);
        if (d.ok) setMessages(d.messages || []);
      } catch {}
    } catch (e) {
      console.error("Stop failed", e);
    } finally {
      setSending(false);
      setPendingApproval(null);
    }
  }, [activeSessionId]);

  const handleApprove = useCallback(async () => {
    if (!pendingApproval) return;
    try { await approveApproval(pendingApproval.approvalId); setPendingApproval(null); } catch (e) { console.error("Approve failed", e); }
  }, [pendingApproval]);

  const handleRejectApproval = useCallback(async () => {
    if (!pendingApproval) return;
    const reason = prompt("请输入拒绝原因（可选）") || undefined;
    try { await rejectApproval(pendingApproval.approvalId, reason); setPendingApproval(null); } catch (e) { console.error("Reject failed", e); }
  }, [pendingApproval]);

  const handleAttach = useCallback(async (file: File) => {
    let sid = activeSessionId;
    if (!sid) {
      try {
        const d = await createChat();
        if (d.sessionId) { sid = d.sessionId; navigateToChat(d.sessionId); } else return;
      } catch { return; }
    }
    try {
      const result = await uploadAttachment(sid, file);
      if (result.ok) {
        const message = result.message || {
          role: "user" as const,
          content: `已上传附件: ${file.name}`,
          command: true,
        };
        setMessages((prev) => [...prev, message]);
      }
    } catch (e) {
      console.error("Upload failed", e);
      setMessages((prev) => [...prev, { role: "system", content: `上传失败: ${file.name}` }]);
    }
  }, [activeSessionId, navigateToChat]);

  const handleToggleSidebar = useCallback(() => {
    if (isMobile) setMobileSidebarOpen((v) => !v);
    else setSidebarCollapsed((v) => !v);
  }, [isMobile]);

  const activeTitle = useMemo(() => {
    if (!activeSessionId) return "SJTUClaw";
    const s = sessions.find((x) => x.sessionId === activeSessionId);
    return s?.title || "SJTUClaw";
  }, [sessions, activeSessionId]);

  const activeUtility = useMemo<ShellView | null>(() => {
    return view === "settings" ? "settings" : null;
  }, [view]);

  const sidebarProps = useMemo(() => ({
    sessions,
    activeSessionId,
    loading: sessionsLoading,
    onNewChat: handleNewChat,
    onSelect: handleSelectSession,
    onDelete: handleDeleteSession,
    onRename: handleRenameSession,
    onOpenSettings: navigateToSettings,
    onToggleSidebar: handleToggleSidebar,
    activeUtility,
    interactionLocked: sending,
  }), [sessions, activeSessionId, sessionsLoading, handleNewChat, handleSelectSession,
      handleDeleteSession, handleRenameSession, navigateToSettings,
      handleToggleSidebar, activeUtility, sending]);

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full overflow-hidden bg-background">
      {/* Ambient background */}
      <div className="ambient-glow" aria-hidden="true" />

      {/* Desktop sidebar */}
      <aside
        className="relative z-10 hidden shrink-0 overflow-hidden transition-[width] duration-300 ease-smooth md:block"
        style={{ width: sidebarCollapsed ? 0 : SIDEBAR_WIDTH }}
      >
        <div className="h-full border-r border-border/70" style={{ width: SIDEBAR_WIDTH }}>
          <Sidebar
            {...sidebarProps}
            collapsed={sidebarCollapsed}
          />
        </div>
      </aside>

      {/* Mobile sidebar overlay */}
      {isMobile && mobileSidebarOpen && (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm md:hidden"
            onClick={() => setMobileSidebarOpen(false)}
          />
          <aside className="fixed inset-y-0 left-0 z-50 w-[min(88vw,288px)] bg-sidebar border-r border-border/70 md:hidden animate-enter-up">
            <Sidebar {...sidebarProps} onToggleSidebar={handleToggleSidebar} />
          </aside>
        </>
      )}

      {/* Main area */}
      <main className="relative z-10 flex-1 min-w-0 flex flex-col">
        <ThreadShell
          sessionId={activeSessionId}
          title={activeTitle}
          messages={messages}
          loading={messagesLoading}
          sending={sending}
          autoMode={autoMode}
          unlimitedMode={unlimitedMode}
          onSend={handleSend}
          onStop={handleStop}
          onAttach={handleAttach}
          onToggleSidebar={handleToggleSidebar}
          onNewChat={handleNewChat}
          theme={theme}
          onToggleTheme={toggleTheme}
        />
      </main>

      {/* Settings floating modal with glass mask */}
      {view === "settings" && (
        <div
          className="fixed inset-0 z-[150] flex items-center justify-center bg-background/40 p-3 backdrop-blur-md sm:p-6"
          onClick={() => navigateToChat(activeSessionId)}
          role="presentation"
        >
          <div
            className="flex h-[88vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-border/70 bg-popover shadow-[0_24px_80px_hsl(215_30%_10%/0.25)] animate-enter-scale"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <SettingsView
              theme={theme}
              activeSection={settingsSection}
              onToggleTheme={toggleTheme}
              onBackToChat={() => navigateToChat(activeSessionId)}
              onSectionChange={setSettingsSection}
              activeSessionId={activeSessionId}
            />
          </div>
        </div>
      )}

      {/* Approval request */}
      {pendingApproval && (
        <div className="fixed inset-0 z-[200] flex items-center justify-center bg-foreground/15 px-4 backdrop-blur-[2px]">
          <div className="w-full max-w-lg rounded-2xl border border-border bg-popover p-5 shadow-[0_24px_80px_hsl(215_30%_10%/0.18)] animate-enter-scale">
            <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-primary">需要确认</p>
                <h2 className="mt-1 text-base font-semibold tracking-tight">允许调用 {pendingApproval.toolName}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">请检查参数。危险操作只有在你批准后才会执行。</p>
                <pre className="mt-4 max-h-48 overflow-auto rounded-xl border border-border/70 bg-secondary/55 p-3 text-[11px] text-foreground/75 whitespace-pre-wrap font-mono text-left">
                  {JSON.stringify(pendingApproval.toolArgs, null, 2)}
                </pre>
            </div>
            <div className="mt-5 flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={handleRejectApproval}>拒绝</Button>
              <Button size="sm" onClick={handleApprove} className="px-5">批准并执行</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <ErrorBoundary>
        <Shell />
      </ErrorBoundary>
    </ThemeProvider>
  );
}
