"""Per-turn iteration budget — thread-safe consume/refund counter.

Follows the ``IterationBudget`` pattern: each agent turn
gets a budget capped at ``max_iterations`` (default from
``CLAW_MAX_AGENT_ITERATIONS`` env var).  The budget is consumed on
every Think-Act-Observe cycle; programmatic tool calls can be refunded
so they don't eat into the limit.

Thread-safe so it can be shared with background compaction workers or
parallel tool executors without races.
"""

from __future__ import annotations

import os
import threading


def _default_max_iterations() -> int:
    """Read the iteration cap from the environment (matches loop.py)."""
    return int(os.getenv("CLAW_MAX_AGENT_ITERATIONS", "15"))


class IterationBudget:
    """Thread-safe iteration counter for one agent turn.

    Each turn gets its own budget. The cap comes from
    ``CLAW_MAX_AGENT_ITERATIONS`` (default 15). ``consume()`` returns
    False when the cap is reached — callers should then invoke the
    no-tools finalization path.

    ``refund()`` gives back one iteration for turns that don't involve
    a real LLM Think-Act cycle (e.g. cached tool results, compaction
    triggers).
    """

    def __init__(self, max_total: int | None = None):
        self.max_total = max_total if max_total is not None else _default_max_iterations()
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for cached/compaction turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    def reset(self) -> None:
        """Reset the counter to zero (e.g. for a fresh turn)."""
        with self._lock:
            self._used = 0

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)

    @property
    def exhausted(self) -> bool:
        """True when no more iterations can be consumed."""
        with self._lock:
            return self._used >= self.max_total

    def to_dict(self) -> dict:
        """Snapshot for metrics/logging."""
        with self._lock:
            return {
                "max_total": self.max_total,
                "used": self._used,
                "remaining": max(0, self.max_total - self._used),
            }


__all__ = ["IterationBudget"]
