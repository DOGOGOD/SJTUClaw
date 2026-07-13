"""Structured per-turn context.

Bundles all the state needed to execute one agent turn into a single
dataclass so it can be passed to helper functions without a long
parameter list. Follows the ``TurnContext`` design.

The :class:`TurnContext` is created at the start of ``run_agent_turn``
and carries:

- The user message and session identity.
- The active skill (if any) and its source.
- The :class:`~claw.agent.budget.IterationBudget` and
  :class:`~claw.agent.metrics.TurnMetrics` for this turn.
- Flags for auto-mode and compaction.

Helpers that previously took 6+ parameters can now accept a single
``TurnContext`` and access what they need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from claw.agent.budget import IterationBudget
from claw.agent.metrics import TurnMetrics


@dataclass
class TurnContext:
    """All state for one agent turn, bundled for easy passing.

    Created at the top of ``run_agent_turn``; consumed by the loop,
    helpers, and the health monitor.
    """

    # -- Identity --
    session_id: str
    user_message: str

    # -- Execution state --
    auto_mode: bool = False
    turn_count: int = 0

    # -- Budget + metrics (created per-turn) --
    budget: IterationBudget = field(default_factory=IterationBudget)
    metrics: TurnMetrics = field(default_factory=lambda: TurnMetrics())

    # -- Recovery flags --
    llm_retry_used: bool = False

    # -- Compaction --
    compaction_triggered: bool = False

    # -- Rejection tracking --
    rejection_tracker: dict[str, int] = field(default_factory=dict)

    # -- Tool result cache (per-turn dedup) --
    tool_result_cache: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Wire up the metrics session_id if not already set.
        if not self.metrics.session_id:
            self.metrics.session_id = self.session_id

    def record_rejection(self, tool_name: str, args: dict) -> int:
        """Record that a tool was rejected and return the new count."""
        import json
        key = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        count = self.rejection_tracker.get(key, 0) + 1
        self.rejection_tracker[key] = count
        return count

    def rejection_count(self, tool_name: str, args: dict) -> int:
        """Return how many times this exact tool+args has been rejected."""
        import json
        key = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        return self.rejection_tracker.get(key, 0)

    def to_dict(self) -> dict[str, Any]:
        """Snapshot for logging/debugging."""
        return {
            "session_id": self.session_id,
            "user_message": self.user_message[:200],
            "auto_mode": self.auto_mode,
            "turn_count": self.turn_count,
            "budget": self.budget.to_dict(),
            "metrics": self.metrics.to_dict(),
            "llm_retry_used": self.llm_retry_used,
            "compaction_triggered": self.compaction_triggered,
            "rejection_tracker_size": len(self.rejection_tracker),
            "tool_cache_size": len(self.tool_result_cache),
        }


__all__ = ["TurnContext"]
