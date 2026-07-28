"""Threshold-triggered session compaction in a background thread.

- Only one compaction runs at a time; concurrent submissions are
  silently skipped.
- Takes a snapshot of the unconsolidated messages under a brief lock
  before the LLM call, so new messages appended during compaction are
  never lost.
- Retries once on an actual summarization failure.
- Treats "nothing can be compacted yet" as a normal no-op.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from claw.config import CompactionConfig
from claw.llm.client import LLMClient
from claw.session.models import Session
from claw.session.store import SessionStore, SessionStoreError

if TYPE_CHECKING:
    from claw.context.compaction import CompactionResult


logger = logging.getLogger(__name__)

CompactionCompletedCallback = Callable[[Session, "CompactionResult"], None]


class CompactionWorker:
    """Background thread that compacts sessions asynchronously.

    The worker takes a **snapshot** of ``session.messages`` under a
    brief lock, then releases the lock before calling the LLM.  This
    means new messages appended during compaction are never lost.

    """

    def __init__(
        self,
        main_llm: LLMClient,
        session_store: SessionStore,
        compact_llm: LLMClient | None = None,
        config: CompactionConfig | None = None,
        session_filter: Callable[[Session], bool] | None = None,
        on_complete: CompactionCompletedCallback | None = None,
    ):
        self._main_llm = main_llm
        self._session_store = session_store
        self._compact_llm = compact_llm or main_llm
        self._config = config or CompactionConfig()
        self._session_filter = session_filter
        self._on_complete = on_complete

        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    # -- public API --------------------------------------------------------

    def submit(self, session: Session) -> bool:
        """Submit *session* for background compaction.

        Returns True if the task was accepted, False if a compaction is
        already in progress (the submission is silently dropped).
        """
        if self._session_filter is not None and not self._session_filter(session):
            return False
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

    def submit_if_needed(self, session: Session) -> bool:
        """Submit after the token threshold and a safe split are both present."""
        from claw.context.compaction import (
            has_compactable_prefix,
            needs_compaction,
        )

        if not needs_compaction(
            session,
            max_message_tokens=self._config.max_message_tokens,
        ):
            return False
        if not has_compactable_prefix(
            session,
            keep_recent_tokens=self._config.keep_recent_tokens,
            keep_recent_messages_min=self._config.keep_recent_messages_min,
        ):
            return False
        return self.submit(session)

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
            logger.exception("[compaction] 后台压缩发生未预期错误")
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
            CompactionNotNeeded,
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
        except CompactionNotNeeded:
            return
        except CompactionError:
            # A real summarization failure may be transient, so retry once.
            try:
                result = compact_session_snapshot(
                    snapshot_messages,
                    snapshot_summary,
                    self._compact_llm,
                    keep_recent_tokens=self._config.keep_recent_tokens,
                    keep_recent_messages_min=self._config.keep_recent_messages_min,
                )
            except CompactionNotNeeded:
                return
            except CompactionError as exc:
                logger.error("[compaction] 后台压缩失败（已重试）: %s", exc)
                return

        # Apply result to the live session (brief lock)
        if self._session_filter is not None and not self._session_filter(session):
            return
        with self._lock:
            # A user turn or rollback changed the session while the LLM was
            # producing this summary.  Applying it would resurrect context
            # from the wrong history branch, so discard it.
            if session.revision != snapshot_revision:
                logger.info("[compaction] session 已变化，丢弃过期的后台压缩结果")
                return
            apply_compaction_result(session, result)

        # Persist
        try:
            self._session_store.save(session)
            logger.info(
                "[compaction] 后台压缩完成: old_messages=%s, recent_messages=%s",
                result.old_message_count,
                result.recent_message_count,
            )
        except SessionStoreError as exc:
            logger.error("[compaction] 压缩完成但保存失败: %s", exc)
            return

        if self._on_complete is not None:
            try:
                self._on_complete(session, result)
            except Exception:
                # A display integration must never turn a successful
                # compaction into a failed background task.
                logger.exception("[compaction] 自动压缩完成通知发送失败")
