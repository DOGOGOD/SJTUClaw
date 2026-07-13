"""Approval manager.

Manages pending approval requests for write/shell tool calls. When the
model requests an update or shell tool, the agent loop creates an
approval and blocks until the user acts on it.

Approval flow::

    agent loop creates ApprovalRequest (status=pending)
      -> user approves  -> execute tool, write result to session
      -> user rejects   -> skip tool, write rejection as observation

Download tool does NOT go through approval — its user confirmation
happens when the user clicks the download link in the frontend.

Reliability:
- Completed approvals are automatically cleaned up after a retention
  period to prevent unbounded memory growth in long-running processes.
- ``cleanup_expired`` can be called periodically to remove stale entries.
"""

from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


from claw.utils import now_iso as _now_iso


def _now_ts() -> float:
    return time.time()


# Retention period for completed approvals (seconds).
# After this, a completed approval is eligible for garbage collection.
_COMPLETED_RETENTION_S = 600  # 10 minutes

# Max number of completed approvals to keep in memory.
# Older ones are pruned first (FIFO).
_MAX_COMPLETED_KEEP = 200


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class ApprovalRequest:
    """A single approval request for a tool call.

    Attributes:
        approval_id: unique id for approve/reject actions.
        session_id: the session this approval belongs to.
        tool_name: the tool being requested.
        tool_args: the arguments the model passed to the tool.
        status: current status (pending / approved / rejected).
        reject_reason: user-provided reason when rejected (optional).
        created_at: ISO-8601 timestamp.
    """

    approval_id: str = field(default_factory=lambda: f"apr_{uuid.uuid4().hex[:12]}")
    session_id: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    status: str = ApprovalStatus.PENDING.value
    reject_reason: str = ""
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approvalId": self.approval_id,
            "sessionId": self.session_id,
            "toolName": self.tool_name,
            "toolArgs": self.tool_args,
            "status": self.status,
            "rejectReason": self.reject_reason,
            "createdAt": self.created_at,
        }


class ApprovalManager:
    """Thread-safe in-memory approval store.

    The agent loop uses ``create()`` to register a pending approval and
    then blocks on ``wait()``. The Gateway (or CLI) calls ``approve()``
    or ``reject()`` to unblock the waiter.

    Usage::

        mgr = ApprovalManager()
        req = mgr.create(session_id, "overwrite_file", {"path": "x.md", ...})
        # ... expose req.approval_id to the user ...
        decision = mgr.wait(req.approval_id, timeout=300)
        if decision.status == "approved":
            result = registry.execute_by_name(...)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._completed_at: dict[str, float] = {}  # approval_id -> completion timestamp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> ApprovalRequest:
        """Create a new pending approval and return it."""
        req = ApprovalRequest(
            session_id=session_id,
            tool_name=tool_name,
            tool_args=tool_args,
        )
        self.register(req)
        return req

    def register(self, req: ApprovalRequest) -> None:
        """Register an already-constructed ``ApprovalRequest``.

        Use this when the request was created outside the manager
        (e.g. by the agent loop) and needs to be tracked for
        approve/reject/wait operations.
        """
        with self._lock:
            # Opportunistic cleanup: prune old completed approvals when
            # the store grows large, preventing unbounded memory growth.
            self._maybe_cleanup_locked()
            self._requests[req.approval_id] = req
            if req.approval_id not in self._events:
                self._events[req.approval_id] = threading.Event()

    def approve(self, approval_id: str) -> ApprovalRequest | None:
        """Approve the request. Unblocks the waiter."""
        with self._lock:
            req = self._requests.get(approval_id)
            if req is None:
                return None
            if req.status != ApprovalStatus.PENDING.value:
                return req
            req.status = ApprovalStatus.APPROVED.value
            self._completed_at[approval_id] = _now_ts()
            evt = self._events.get(approval_id)
            if evt:
                evt.set()
        return req

    def reject(
        self, approval_id: str, reason: str = ""
    ) -> ApprovalRequest | None:
        """Reject the request with an optional reason. Unblocks the waiter."""
        with self._lock:
            req = self._requests.get(approval_id)
            if req is None:
                return None
            if req.status != ApprovalStatus.PENDING.value:
                return req
            req.status = ApprovalStatus.REJECTED.value
            req.reject_reason = reason
            self._completed_at[approval_id] = _now_ts()
            evt = self._events.get(approval_id)
            if evt:
                evt.set()
        return req

    def wait(
        self, approval_id: str, timeout: float = 300.0
    ) -> ApprovalRequest | None:
        """Block until the approval is decided or *timeout* expires.

        Returns the final ``ApprovalRequest`` (approved/rejected), or
        None if timed out.
        """
        evt = self._events.get(approval_id)
        if evt is None:
            return None
        signaled = evt.wait(timeout=timeout)
        with self._lock:
            if not signaled:
                # Timeout — treat as rejection
                req = self._requests.get(approval_id)
                if req and req.status == ApprovalStatus.PENDING.value:
                    req.status = ApprovalStatus.REJECTED.value
                    req.reject_reason = "审批超时，自动拒绝"
                    self._completed_at[approval_id] = _now_ts()
                return req
            return self._requests.get(approval_id)

    def get_pending(self) -> list[ApprovalRequest]:
        """Return all currently pending approvals."""
        with self._lock:
            return [
                r
                for r in self._requests.values()
                if r.status == ApprovalStatus.PENDING.value
            ]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        """Return the request by id or None."""
        with self._lock:
            return self._requests.get(approval_id)

    def list_by_session(self, session_id: str) -> list[ApprovalRequest]:
        """Return all approvals for a given session (any status)."""
        with self._lock:
            return [
                r
                for r in self._requests.values()
                if r.session_id == session_id
            ]

    def cleanup_expired(self) -> int:
        """Remove completed approvals older than the retention period.

        Returns the number of entries removed.  Safe to call from any
        thread; callers may invoke this periodically (e.g. from the cron
        service) to bound memory usage.
        """
        with self._lock:
            return self._maybe_cleanup_locked(force=True)

    # ------------------------------------------------------------------
    # Internal cleanup
    # ------------------------------------------------------------------

    def _maybe_cleanup_locked(self, *, force: bool = False) -> int:
        """Prune old completed approvals. Caller must hold ``self._lock``.

        Two strategies:
        1. Time-based: remove entries completed > ``_COMPLETED_RETENTION_S``
           ago.
        2. Count-based: if completed entries exceed
           ``_MAX_COMPLETED_KEEP``, remove oldest first.

        When *force* is False (called opportunistically on ``create``),
        only runs when the store has grown past a threshold.  When True
        (called from ``cleanup_expired``), always runs.
        """
        if not force and len(self._requests) < _MAX_COMPLETED_KEEP:
            return 0

        now = _now_ts()
        to_remove: list[str] = []

        # Time-based: remove old completed entries
        for aid, ts in self._completed_at.items():
            if now - ts > _COMPLETED_RETENTION_S:
                to_remove.append(aid)

        # Count-based: if still too many, prune oldest completed
        if len(self._requests) - len(to_remove) > _MAX_COMPLETED_KEEP:
            remaining_completed = [
                (aid, ts) for aid, ts in self._completed_at.items()
                if aid not in to_remove
            ]
            remaining_completed.sort(key=lambda x: x[1])
            excess = len(self._requests) - len(to_remove) - _MAX_COMPLETED_KEEP
            for aid, _ in remaining_completed[:excess]:
                to_remove.append(aid)

        for aid in to_remove:
            self._requests.pop(aid, None)
            self._events.pop(aid, None)
            self._completed_at.pop(aid, None)

        return len(to_remove)
