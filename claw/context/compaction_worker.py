"""Async compaction worker: runs session compaction in a background thread.

v3 changes:
- Idle session auto-compaction: periodically checks for sessions that
  haven't been touched in a configurable TTL and hard-truncates them.
- Only one compaction runs at a time; concurrent submissions are
  silently skipped.
- Takes a snapshot of ``session.messages`` under a brief lock before
  the LLM call, so new messages appended during compaction are never
  lost.
- Retries once on LLM failure.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone

from claw.config import CompactionConfig
from claw.llm.client import LLMClient
from claw.session.models import Session
from claw.session.store import SessionStore, SessionStoreError


class CompactionWorker:
    """Background thread that compacts sessions asynchronously.

    The worker takes a **snapshot** of ``session.messages`` under a
    brief lock, then releases the lock before calling the LLM.  This
    means new messages appended during compaction are never lost.

    When ``idle_ttl_minutes`` is set, the worker also periodically
    scans for sessions that have been idle longer than the TTL and
    hard-truncates them.
    """

    def __init__(
        self,
        main_llm: LLMClient,
        session_store: SessionStore,
        compact_llm: LLMClient | None = None,
        config: CompactionConfig | None = None,
        idle_ttl_minutes: int = 0,
    ):
        self._main_llm = main_llm
        self._session_store = session_store
        self._compact_llm = compact_llm or main_llm
        self._config = config or CompactionConfig()
        self._idle_ttl_minutes = idle_ttl_minutes

        self._lock = threading.Lock()
        self._running = False
        self._idle_running = False
        self._thread: threading.Thread | None = None
        self._idle_thread: threading.Thread | None = None

    # -- public API --------------------------------------------------------

    def submit(self, session: Session) -> bool:
        """Submit *session* for background compaction.

        Returns True if the task was accepted, False if a compaction is
        already in progress (the submission is silently dropped).
        """
        with self._lock:
            if self._running:
                return False
            self._running = True

            # Take a snapshot under the lock
            snapshot_messages = list(session.get_unconsolidated_messages())
            snapshot_summary = session.summary
            snapshot_revision = session.revision

        self._thread = threading.Thread(
            target=self._run,
            args=(session, snapshot_messages, snapshot_summary, snapshot_revision),
            daemon=True,
        )
        self._thread.start()
        return True

    def start_idle_compaction(self) -> None:
        """Start the idle-session compaction background loop.

        Only active when ``idle_ttl_minutes > 0``.
        """
        if self._idle_ttl_minutes <= 0:
            return
        if self._idle_running:
            return
        self._idle_running = True
        self._idle_thread = threading.Thread(
            target=self._idle_loop,
            daemon=True,
            name="claw-idle-compaction",
        )
        self._idle_thread.start()

    def stop_idle_compaction(self) -> None:
        """Stop the idle-session compaction background loop."""
        self._idle_running = False
        if self._idle_thread is not None:
            self._idle_thread.join(timeout=5)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the current compaction to finish.

        Returns True if the worker is idle (no task was running or it
        already finished), False if the timeout expired.
        """
        thread: threading.Thread | None = None
        with self._lock:
            thread = self._thread

        if thread is None:
            return True

        thread.join(timeout=timeout)
        return not thread.is_alive()

    def is_running(self) -> bool:
        """Return True if a compaction task is currently executing."""
        with self._lock:
            return self._running

    # -- internal ----------------------------------------------------------

    def _run(
        self,
        session: Session,
        snapshot_messages: list,
        snapshot_summary: str,
        snapshot_revision: int,
    ) -> None:
        try:
            self._do_compact(session, snapshot_messages, snapshot_summary, snapshot_revision)
        except Exception:
            traceback.print_exc()
        finally:
            with self._lock:
                self._running = False
                self._thread = None

    def _do_compact(
        self,
        session: Session,
        snapshot_messages: list,
        snapshot_summary: str,
        snapshot_revision: int,
    ) -> None:
        from claw.context.compaction import (
            CompactionError,
            apply_compaction_result,
            compact_session_snapshot,
        )

        try:
            result = compact_session_snapshot(
                snapshot_messages,
                snapshot_summary,
                self._compact_llm,
                keep_recent_tokens=self._config.keep_recent_tokens,
                keep_recent_messages_min=self._config.keep_recent_messages_min,
            )
        except CompactionError:
            # First attempt failed — retry once
            try:
                result = compact_session_snapshot(
                    snapshot_messages,
                    snapshot_summary,
                    self._compact_llm,
                    keep_recent_tokens=self._config.keep_recent_tokens,
                    keep_recent_messages_min=self._config.keep_recent_messages_min,
                )
            except CompactionError as exc:
                print(f"[compaction] 后台压缩失败（已重试）: {exc}")
                return

        # Apply result to the live session (brief lock)
        with self._lock:
            # A user turn or rollback changed the session while the LLM was
            # producing this summary.  Applying it would resurrect context
            # from the wrong history branch, so discard it.
            if session.revision != snapshot_revision:
                print("[compaction] session 已变化，丢弃过期的后台压缩结果")
                return
            apply_compaction_result(session, result)

        # Persist
        try:
            self._session_store.save(session)
            print(
                f"[compaction] 后台压缩完成: "
                f"old_messages={result.old_message_count}, "
                f"recent_messages={result.recent_message_count}"
            )
        except SessionStoreError as exc:
            print(f"[compaction] 压缩完成但保存失败: {exc}")

    # -- idle compaction loop ----------------------------------------------

    def _idle_loop(self) -> None:
        """Periodically scan for idle sessions and hard-truncate them.

        Only compaction trigger is idle-session scanning.
        No per-turn proactive compaction or token-threshold checks.
        """
        # Check every 2 minutes (more responsive than an event-driven model)
        poll_interval = 120

        while self._idle_running:
            try:
                self._idle_tick()
            except Exception:
                traceback.print_exc()
            time.sleep(poll_interval)

    def _idle_tick(self) -> None:
        """Scan for sessions idle longer than TTL and compact them."""
        if self._idle_ttl_minutes <= 0:
            return

        now = datetime.now(timezone.utc)
        summaries = self._session_store.list_summaries()

        for s in summaries:
            try:
                updated = datetime.fromisoformat(s.updated_at)
                age_minutes = (now - updated).total_seconds() / 60
            except (ValueError, TypeError):
                continue

            if age_minutes < self._idle_ttl_minutes:
                continue

            # Load and check
            try:
                session = self._session_store.get(s.session_id)
            except SessionStoreError:
                continue

            from claw.context.compaction import has_compactable_idle_tail

            if not has_compactable_idle_tail(session, max_suffix=8):
                continue

            # Hard-truncate this idle session
            try:
                from claw.context.compaction import compact_idle_session
                summary = compact_idle_session(
                    s.session_id,
                    self._session_store,
                    self._compact_llm,
                    max_suffix=8,
                )
                if summary:
                    print(
                        f"[idle-compaction] 已压缩空闲 session "
                        f"{s.session_id} (idle {age_minutes:.0f}min)"
                    )
            except Exception:
                traceback.print_exc()
