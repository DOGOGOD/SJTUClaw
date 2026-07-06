"""Background scheduler: polls for due tasks and invokes ``run_agent_turn``.

Boundary strategies (also documented in ``tasks_store.py``):

1. **Single execution failure → continue next trigger.**
   A failed execution records the error in ``executionHistory`` but does
   NOT prevent the next scheduled run (for periodic tasks). One-time
   tasks that fail are marked ``failed`` and never retried automatically.

2. **Execution exceeds interval.**
   For interval tasks, the next ``next_run_at`` is calculated as
   ``now + interval_seconds`` after each execution completes. If
   execution takes longer than the interval, the next run becomes
   immediately due on the next poll cycle — it does not "skip".

3. **Cancel → stops all future triggers.**
   Cancelling a task sets its status to ``cancelled`` and clears
   ``next_run_at``. The scheduler ignores cancelled tasks forever;
   a cancelled task can only be restored by creating a new one.

4. **Missed triggers during shutdown.**
   On restart, any ``waiting`` task whose ``next_run_at`` ≤ now will
   fire during the next poll cycle. This means tasks that were due
   while the program was shut down are executed immediately after
   restart (best-effort catch-up). If you prefer to skip past-due
   tasks, cancel them before shutdown or manually adjust the data.

The scheduler runs in its own daemon thread so it never blocks the
main process from shutting down.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone

from claw.agent.loop import run_agent_turn
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.scheduler.tasks_store import TasksStore
from claw.session.store import SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry

POLL_INTERVAL_SECONDS = 15  # How often the scheduler checks for due tasks


class Scheduler:
    """Background thread that polls ``TasksStore`` and executes due tasks.

    Usage::

        sched = Scheduler(tasks_store, session_store, context_builder,
                          tool_registry, llm_client)
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
    ):
        self._tasks_store = tasks_store
        self._session_store = session_store
        self._context_builder = context_builder
        self._tool_registry = tool_registry
        self._llm_client = llm_client

        self._running = False
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="claw-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- main loop -----------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception:
                # Never let a single tick crash the scheduler thread
                traceback.print_exc()
            time.sleep(POLL_INTERVAL_SECONDS)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        due = self._tasks_store.get_due(now)
        for task in due:
            if not self._running:
                return
            self._execute_task(task)

    def _execute_task(self, task) -> None:
        tid = task.task_id
        self._tasks_store.mark_running(tid)

        # Resolve session — create if missing (deleted by user, etc.)
        sid = task.session_id
        if not self._session_store.exists(sid):
            try:
                sess = self._session_store.create_session(session_id=sid)
            except SessionStoreError:
                sid = sess.session_id if sess else "default"

        try:
            reply = run_agent_turn(
                sid,
                task.content,
                session_store=self._session_store,
                context_builder=self._context_builder,
                tool_registry=self._tool_registry,
                llm_client=self._llm_client,
            )
        except (LLMError, SessionStoreError) as exc:
            self._tasks_store.record_failure(tid, str(exc))
            return
        except Exception as exc:
            self._tasks_store.record_failure(
                tid, f"未预期的异常: {exc}\n{traceback.format_exc()}"
            )
            return

        self._tasks_store.record_success(tid, reply)
