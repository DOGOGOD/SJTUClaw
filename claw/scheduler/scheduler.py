"""Background scheduler — v6.

Enhancements over v5:

- **Concurrency control**: per-job max_concurrent prevents duplicate
  execution; PID tracking detects and recovers stale "running" tasks.
- **Run records**: structured RunRecord with run_id, started_at,
  completed_at, duration_ms.
- **Deferred execution**: defer_until_idle waits for session to become
  idle before dispatching.
- **Timeout**: per-job timeout_seconds; if exceeded, the task is
  marked as "timeout" and released.
- **Stale recovery**: on startup, stale "running" tasks from crashed
  processes are reset to "waiting".
- **Auto-cleanup**: completed/failed once tasks pruned after 30 days.
- **Priority queue**: due tasks sorted by next_run_at.
- **Parallel execution**: configurable max_parallel limit.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from claw.agent.loop import run_agent_turn
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.scheduler.tasks_store import TasksStore, TasksStoreError
from claw.session.store import SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 10       # how often the scheduler checks for due tasks
DEFAULT_MAX_PARALLEL = 3         # max concurrent task executions
CLEANUP_INTERVAL_SECONDS = 3600  # clean up stale tasks every hour


class Scheduler:
    """Background thread scheduler — v6.

    Usage::

        sched = Scheduler(tasks_store, session_store, context_builder,
                          tool_registry, llm_client, max_parallel=3)
        sched.start()
        ...
        sched.stop()
    """

    def __init__(
        self,
        tasks_store: TasksStore,
        session_store: SessionStore,
        context_builder: ContextBuilder,
        tool_registry: ToolRegistry,
        llm_client: LLMClient,
        *,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
    ):
        self._tasks_store = tasks_store
        self._session_store = session_store
        self._context_builder = context_builder
        self._tool_registry = tool_registry
        self._llm_client = llm_client
        self._max_parallel = max_parallel

        self._running = False
        self._thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._active_count = 0
        self._active_lock = threading.Lock()

        # Track active session keys for defer_until_idle
        self._active_session_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="claw-scheduler",
        )
        self._thread.start()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="claw-scheduler-cleanup",
        )
        self._cleanup_thread.start()

    def stop(self) -> None:
        self._running = False
        for t in (self._thread, self._cleanup_thread):
            if t is not None:
                t.join(timeout=5)

    # ------------------------------------------------------------------
    # Active session tracking (for defer_until_idle)
    # ------------------------------------------------------------------

    def mark_session_active(self, session_key: str) -> None:
        """Mark a session as having an active turn."""
        self._active_session_keys.add(session_key)

    def mark_session_idle(self, session_key: str) -> None:
        """Mark a session as idle (turn completed)."""
        self._active_session_keys.discard(session_key)

    def is_session_idle(self, session_key: str) -> bool:
        return session_key not in self._active_session_keys

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                traceback.print_exc()
            time.sleep(POLL_INTERVAL_SECONDS)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        due = self._tasks_store.get_due(now)

        # Sort by next_run_at (oldest first)
        due.sort(key=lambda t: t.next_run_at)

        for task in due:
            if not self._running:
                return

            # Concurrency gate
            with self._active_lock:
                if self._active_count >= self._max_parallel:
                    break

            # Deferred execution: skip if session is busy
            if task.defer_until_idle and not self.is_session_idle(task.session_id):
                continue

            # Try to acquire the task
            if not self._tasks_store.try_acquire(task.task_id):
                continue

            # Execute in a daemon thread
            t = threading.Thread(
                target=self._execute_task,
                args=(task,),
                daemon=True,
                name=f"claw-task-{task.task_id}",
            )
            with self._active_lock:
                self._active_count += 1
            t.start()

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    def _execute_task(self, task) -> None:
        tid = task.task_id
        started_at = time.time()

        try:
            # Resolve session — create if missing
            sid = task.session_id
            if not self._session_store.exists(sid):
                try:
                    sess = self._session_store.create_session(session_id=sid)
                    sid = sess.session_id
                except SessionStoreError:
                    pass

            self.mark_session_active(sid)

            # Execute with optional timeout
            if task.timeout_seconds > 0:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        run_agent_turn,
                        sid,
                        task.content,
                        session_store=self._session_store,
                        context_builder=self._context_builder,
                        tool_registry=self._tool_registry,
                        llm_client=self._llm_client,
                    )
                    try:
                        reply = future.result(timeout=task.timeout_seconds)
                    except concurrent.futures.TimeoutError:
                        duration_ms = int((time.time() - started_at) * 1000)
                        self._tasks_store.record_failure(
                            tid,
                            f"任务执行超时（{task.timeout_seconds}s）",
                            duration_ms=duration_ms,
                        )
                        self.mark_session_idle(sid)
                        return
            else:
                reply = run_agent_turn(
                    sid,
                    task.content,
                    session_store=self._session_store,
                    context_builder=self._context_builder,
                    tool_registry=self._tool_registry,
                    llm_client=self._llm_client,
                )

            duration_ms = int((time.time() - started_at) * 1000)
            self._tasks_store.record_success(tid, reply, duration_ms=duration_ms)

        except (LLMError, SessionStoreError) as exc:
            duration_ms = int((time.time() - started_at) * 1000)
            self._tasks_store.record_failure(tid, str(exc), duration_ms=duration_ms)
        except Exception as exc:
            duration_ms = int((time.time() - started_at) * 1000)
            self._tasks_store.record_failure(
                tid,
                f"未预期的异常: {exc}\n{traceback.format_exc()}",
                duration_ms=duration_ms,
            )
        finally:
            self.mark_session_idle(task.session_id)
            with self._active_lock:
                self._active_count = max(0, self._active_count - 1)

    # ------------------------------------------------------------------
    # Cleanup loop
    # ------------------------------------------------------------------

    def _cleanup_loop(self) -> None:
        while self._running:
            try:
                removed = self._tasks_store.cleanup_stale()
                if removed > 0:
                    print(f"[scheduler] 清理了 {removed} 个过期的一次性任务")
            except Exception:
                pass
            time.sleep(CLEANUP_INTERVAL_SECONDS)
