import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Sidebar } from "@/components/sidebar/Sidebar";
import { ThreadShell } from "@/components/thread/ThreadShell";
import { SettingsView } from "@/components/settings/SettingsView";
import { ThemeProvider, useTheme } from "@/hooks/useTheme";
import { useSessions } from "@/hooks/useSessions";
import { cn } from "@/lib/utils";
import { fetchMessages, sendMessage, sendCommand, uploadAttachment, renameSession, fetchApprovals, approveApproval, rejectApproval } from "@/lib/api";
import type { ApprovalInfo } from "@/lib/types";
import type { ChatMessage, SettingsSection, ShellView } from "@/lib/types";

const SIDEBAR_WIDTH = 272;
const SIDEBAR_COLLAPSED_WIDTH = 0;

function Shell() {
  const { theme, toggle: toggleTheme } = useTheme();
  const { sessions, loading: sessionsLoading, refresh: refreshSessions, createChat, deleteChat } = useSessions();

  // Routing state
  const [view, setView] = useState<ShellView>("chat");
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("prompt");

  // Chat state
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [sending, setSending] = useState(false);

  // UI state
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<ApprovalInfo | null>(null);
  const [autoMode, setAutoMode] = useState(false);

  // Detect mobile
  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768);
    check();
    window.addEventListener("resize", check);
    return () => window.removeEventListener("resize", check);
  }, []);

  // Load messages when session changes (with cancellation to prevent races)
  useEffect(() => {
    if (!activeSessionId) {
      setMessages([]);
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
        } else {
          setMessages([]);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        console.error("Failed to load messages", e);
        setMessages([]);
      })
      .finally(() => {
        if (!cancelled) setMessagesLoading(false);
      });
    return () => { cancelled = true; };
  }, [activeSessionId]);

  // Navigation
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

  // Actions
  const handleNewChat = useCallback(async () => {
    try {
      const d = await createChat();
      if (d.sessionId) {
        navigateToChat(d.sessionId);
      }
    } catch (e) {
      console.error("Failed to create chat", e);
    }
  }, [createChat, navigateToChat]);

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      navigateToChat(sessionId);
    },
    [navigateToChat]
  );

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      await deleteChat(sessionId);
      if (activeSessionId === sessionId) {
        navigateToChat(null);
      }
    },
    [deleteChat, activeSessionId, navigateToChat]
  );

  const handleRenameSession = useCallback(
    async (sessionId: string) => {
      const title = prompt("新标题：");
      if (!title?.trim()) return;
      try {
        await renameSession(sessionId, title.trim());
        await refreshSessions();
      } catch {}
    },
    [refreshSessions]
  );

  const handleSend = useCallback(
    async (message: string) => {
      if (!activeSessionId) return;
      setSending(true);
      const userMsg: ChatMessage = { role: "user", content: message };
      setMessages((prev) => [...prev, userMsg]);

      const isCommand = /^\/(session|memory|compact|exit|task|workspace|approve|reject|approvals|reflect|skill|help|auto)\b/.test(message);

      // Poll for approvals in parallel while waiting for /chat to return.
      // This breaks the deadlock: /chat blocks waiting for approval, but
      // polling runs concurrently so the frontend can show & act on approvals.
      let pollTimer: ReturnType<typeof setInterval> | null = null;
      const stopPolling = () => {
        if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null; }
      };

      if (!isCommand) {
        pollTimer = setInterval(async () => {
          try {
            const ad = await fetchApprovals(activeSessionId);
            if (ad.approvals?.length > 0) {
              setPendingApproval(ad.approvals[0]);
            }
          } catch {}
        }, 1500);
      }

      try {
        let ok = false;
        if (isCommand) {
          const d = await sendCommand({ sessionId: activeSessionId, command: message });
          if (d.ok && d.result) {
            setMessages((prev) => [...prev, { role: "assistant", content: d.result }]);
            // Track auto mode from command responses
            if (message.startsWith("/auto") && d.result.includes("已开启")) {
              setAutoMode(true);
            } else if (message.startsWith("/auto") && d.result.includes("已关闭")) {
              setAutoMode(false);
            }
            ok = true;
          }
        } else {
          const d = await sendMessage({ sessionId: activeSessionId, message });
          if (d.ok && d.reply) {
            setMessages((prev) => [...prev, { role: "assistant", content: d.reply }]);
            ok = true;
          }
          // Track auto mode from chat response
          if ((d as any).autoMode !== undefined) {
            setAutoMode(!!(d as any).autoMode);
          }
        }
        if (!ok) {
          setMessages((prev) => prev.slice(0, -1));
        }
        if (ok) await refreshSessions();
      } catch (e) {
        console.error("Send failed", e);
        setMessages((prev) => prev.slice(0, -1));
      } finally {
        stopPolling();
        setPendingApproval(null);
        setSending(false);
      }
    },
    [activeSessionId, refreshSessions]
  );

  const handleApprove = useCallback(async () => {
    if (!pendingApproval) return;
    try {
      await approveApproval(pendingApproval.approvalId);
      setPendingApproval(null);
    } catch (e) {
      console.error("Approve failed", e);
    }
  }, [pendingApproval]);

  const handleRejectApproval = useCallback(async () => {
    if (!pendingApproval) return;
    const reason = prompt("拒绝原因（可选）：") || undefined;
    try {
      await rejectApproval(pendingApproval.approvalId, reason);
      setPendingApproval(null);
    } catch (e) {
      console.error("Reject failed", e);
    }
  }, [pendingApproval]);

  const handleAttach = useCallback(
    async (file: File) => {
      let sid = activeSessionId;
      // Auto-create a session if none is selected
      if (!sid) {
        try {
          const d = await createChat();
          if (d.sessionId) {
            sid = d.sessionId;
            navigateToChat(d.sessionId);
          } else {
            return;
          }
        } catch {
          return;
        }
      }
      try {
        const result = await uploadAttachment(sid, file);
        if (result.ok) {
          console.log(`[upload] ${file.name} 上传成功`);
          // Add a system message to show the upload
          setMessages((prev) => [
            ...prev,
            { role: "system", content: `附件已上传: ${file.name}` },
          ]);
        }
      } catch (e) {
        console.error("Upload failed", e);
        setMessages((prev) => [
          ...prev,
          { role: "system", content: `附件上传失败: ${file.name}` },
        ]);
      }
    },
    [activeSessionId, navigateToChat]
  );

  const handleToggleSidebar = useCallback(() => {
    if (isMobile) {
      setMobileSidebarOpen((v) => !v);
    } else {
      setSidebarCollapsed((v) => !v);
    }
  }, [isMobile]);

  const handleToggleCollapse = useCallback(() => {
    setSidebarCollapsed((v) => !v);
  }, []);

  const activeTitle = useMemo(() => {
    if (!activeSessionId) return "SJTUClaw";
    const s = sessions.find((x) => x.sessionId === activeSessionId);
    return s?.title || "SJTUClaw";
  }, [sessions, activeSessionId]);

  const activeUtility = useMemo<ShellView | null>(() => {
    return view === "settings" ? "settings" : null;
  }, [view]);

  const sidebarProps = {
    sessions,
    activeSessionId,
    loading: sessionsLoading,
    onNewChat: handleNewChat,
    onSelect: handleSelectSession,
    onDelete: handleDeleteSession,
    onRename: handleRenameSession,
    onOpenSettings: navigateToSettings,
    onToggleSidebar: handleToggleSidebar,
    onToggleCollapse: handleToggleCollapse,
    activeUtility,
  };

  return (
    <div className="flex h-full w-full overflow-hidden bg-background">
      {/* Desktop sidebar */}
      <aside
        className="hidden shrink-0 overflow-hidden border-r border-border transition-[width] duration-300 ease-out md:block"
        style={{ width: sidebarCollapsed ? 56 : SIDEBAR_WIDTH }}
      >
        <div className="h-full" style={{ width: sidebarCollapsed ? 56 : SIDEBAR_WIDTH }}>
          <Sidebar
            {...sidebarProps}
            collapsed={sidebarCollapsed}
            onToggleCollapse={handleToggleCollapse}
          />
        </div>
      </aside>

      {/* Mobile sidebar overlay */}
      {isMobile && mobileSidebarOpen && (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/50 md:hidden"
            onClick={() => setMobileSidebarOpen(false)}
          />
          <aside className="fixed inset-y-0 left-0 z-50 w-[272px] bg-sidebar border-r border-border md:hidden">
            <Sidebar {...sidebarProps} onToggleSidebar={handleToggleSidebar} />
          </aside>
        </>
      )}

      {/* Approval banner */}
      {pendingApproval && (
        <div className="fixed bottom-24 left-1/2 z-50 -translate-x-1/2 max-w-lg w-[calc(100%-2rem)] rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950 dark:border-amber-700 p-4 shadow-xl animate-fade-in">
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold">
                等待审批：{pendingApproval.toolName}
              </p>
              <pre className="mt-1 max-h-24 overflow-auto text-[11px] text-muted-foreground whitespace-pre-wrap font-mono">
                {JSON.stringify(pendingApproval.toolArgs, null, 2)}
              </pre>
            </div>
            <div className="flex gap-2 shrink-0">
              <Button size="sm" onClick={handleApprove}>批准</Button>
              <Button variant="destructive" size="sm" onClick={handleRejectApproval}>拒绝</Button>
            </div>
          </div>
        </div>
      )}

      {/* Main area */}
      <main className="flex-1 min-w-0 flex flex-col">
        {view === "chat" && (
          <ThreadShell
            sessionId={activeSessionId}
            title={activeTitle}
            messages={messages}
            loading={messagesLoading}
            sending={sending}
            autoMode={autoMode}
            onSend={handleSend}
            onAttach={handleAttach}
            onToggleSidebar={handleToggleSidebar}
            onNewChat={handleNewChat}
            theme={theme}
            onToggleTheme={toggleTheme}
            hideSidebarToggle={true}
          />
        )}
        {view === "settings" && (
          <SettingsView
            theme={theme}
            activeSection={settingsSection}
            onToggleTheme={toggleTheme}
            onBackToChat={() => navigateToChat(activeSessionId)}
            onSectionChange={setSettingsSection}
            activeSessionId={activeSessionId}
          />
        )}
      </main>
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
