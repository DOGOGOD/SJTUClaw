"""Loop health monitoring and auto-recovery.

The :class:`LoopHealthMonitor` watches the agent loop across turns and
detects unhealthy patterns:

- **Error storms**: high error rate over recent turns.
- **Stuck loops**: the same tool called repeatedly without progress.
- **Context overflow**: frequent compaction triggers.
- **Budget exhaustion**: turns that hit the iteration cap.
- **LLM instability**: frequent retries or truncation recoveries.

When an unhealthy pattern is detected, the monitor returns a
:class:`HealthAlert` with a recommended recovery action. Callers can
then apply the action (e.g. trigger compaction, increase budget,
disable a misbehaving tool) or surface it to the user.

The monitor is stateless across sessions -- it only tracks recent
turns in a rolling window. Persist it as long as the session lives.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from claw.agent.metrics import TurnMetrics


@dataclass
class HealthAlert:
    """One detected health issue with a recommended action."""

    level: str  # "info" | "warning" | "critical"
    category: str  # "error_storm" | "stuck_loop" | "context_overflow" | ...
    message: str
    recommended_action: str  # "compact" | "increase_budget" | "disable_tool" | ...
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "recommended_action": self.recommended_action,
            "details": self.details,
        }


class LoopHealthMonitor:
    """Monitor agent loop health across turns.

    Maintains a rolling window of recent turn metrics and checks for
    unhealthy patterns after each turn completes. Call
    :meth:`record_turn` after ``run_agent_turn`` returns, then call
    :meth:`check_health` to get any active alerts.

    The window size (default 10 turns) controls how far back the
    monitor looks for patterns. A smaller window is more reactive;
    a larger window is more stable.
    """

    # Thresholds (tuned for a 10-turn window).
    _ERROR_RATE_WARN = 0.3       # 30% of calls failing
    _ERROR_RATE_CRITICAL = 0.5   # 50% of calls failing
    _MAX_ITER_WARN_TURNS = 3     # 3+ turns hitting the cap in window
    _COMPACTION_WARN_TURNS = 3   # 3+ compactions in window
    _LLM_RETRY_WARN_TURNS = 3    # 3+ retries in window
    _STUCK_TOOL_REPEAT = 4       # same tool 4+ times in one turn

    def __init__(self, window_size: int = 10):
        self._window_size = window_size
        self._turns: deque[dict[str, Any]] = deque(maxlen=window_size)
        self._lock = threading.Lock()

    def record_turn(self, metrics: TurnMetrics) -> None:
        """Add a completed turn's metrics to the monitoring window."""
        with self._lock:
            self._turns.append(metrics.to_dict())

    def check_health(self) -> list[HealthAlert]:
        """Return all active health alerts (empty list = healthy)."""
        with self._lock:
            turns = list(self._turns)

        if not turns:
            return []

        alerts: list[HealthAlert] = []

        # -- 1. Error storm detection --
        total_errors = sum(t["llm"]["errors"] + t["tools"]["errors"] for t in turns)
        total_calls = sum(t["llm"]["calls"] + t["tools"]["calls"] for t in turns)
        if total_calls > 0:
            error_rate = total_errors / total_calls
            if error_rate >= self._ERROR_RATE_CRITICAL:
                alerts.append(HealthAlert(
                    level="critical",
                    category="error_storm",
                    message=f"Error rate {error_rate:.0%} over last {len(turns)} turns",
                    recommended_action="investigate",
                    details={"error_rate": round(error_rate, 3), "errors": total_errors, "calls": total_calls},
                ))
            elif error_rate >= self._ERROR_RATE_WARN:
                alerts.append(HealthAlert(
                    level="warning",
                    category="error_storm",
                    message=f"Error rate {error_rate:.0%} over last {len(turns)} turns",
                    recommended_action="monitor",
                    details={"error_rate": round(error_rate, 3), "errors": total_errors, "calls": total_calls},
                ))

        # -- 2. Budget exhaustion detection --
        max_iter_turns = sum(1 for t in turns if t["max_iterations_reached"])
        if max_iter_turns >= self._MAX_ITER_WARN_TURNS:
            alerts.append(HealthAlert(
                level="warning",
                category="budget_exhaustion",
                message=f"{max_iter_turns}/{len(turns)} turns hit the iteration cap",
                recommended_action="increase_budget",
                details={"max_iter_turns": max_iter_turns, "window": len(turns)},
            ))

        # -- 3. Context overflow detection --
        compaction_turns = sum(1 for t in turns if t["compaction_triggered"])
        truncation_turns = sum(1 for t in turns if t["context_truncated"])
        if compaction_turns >= self._COMPACTION_WARN_TURNS:
            alerts.append(HealthAlert(
                level="warning",
                category="context_overflow",
                message=f"{compaction_turns} compactions in last {len(turns)} turns",
                recommended_action="compact",
                details={"compaction_turns": compaction_turns, "truncation_turns": truncation_turns},
            ))

        # -- 4. LLM instability detection --
        retry_turns = sum(1 for t in turns if t["llm"]["retries"] > 0)
        truncated_turns = sum(1 for t in turns if t["llm"]["truncated"] > 0)
        if retry_turns >= self._LLM_RETRY_WARN_TURNS:
            alerts.append(HealthAlert(
                level="warning",
                category="llm_instability",
                message=f"{retry_turns} turns needed LLM retries in last {len(turns)} turns",
                recommended_action="check_llm_config",
                details={"retry_turns": retry_turns, "truncated_turns": truncated_turns},
            ))

        # -- 5. Per-turn stuck-loop detection (intra-turn tool repeat) --
        # This checks if any single turn had an unusually high tool call count
        # relative to its iteration count -- a sign of the LLM calling the same
        # tool repeatedly without making progress.
        for t in turns:
            if t["iterations"] > 0:
                tools_per_iter = t["tools"]["calls"] / t["iterations"]
                if tools_per_iter >= self._STUCK_TOOL_REPEAT:
                    alerts.append(HealthAlert(
                        level="warning",
                        category="stuck_loop",
                        message=(
                            f"Turn {t.get('turn_id', '?')} had "
                            f"{t['tools']['calls']} tool calls across "
                            f"{t['iterations']} iterations"
                        ),
                        recommended_action="investigate",
                        details={
                            "tool_calls": t["tools"]["calls"],
                            "iterations": t["iterations"],
                            "tools_per_iter": round(tools_per_iter, 2),
                        },
                    ))
                    break  # one alert is enough to flag the pattern

        return alerts

    def summary(self) -> dict[str, Any]:
        """Return a health summary for dashboards/logging."""
        with self._lock:
            turns = list(self._turns)

        if not turns:
            return {"turns_monitored": 0, "status": "no_data"}

        alerts = self.check_health()
        total_errors = sum(t["llm"]["errors"] + t["tools"]["errors"] for t in turns)
        total_calls = sum(t["llm"]["calls"] + t["tools"]["calls"] for t in turns)
        avg_duration = sum(t["duration_ms"] for t in turns) / len(turns)

        status = "healthy"
        if any(a.level == "critical" for a in alerts):
            status = "critical"
        elif any(a.level == "warning" for a in alerts):
            status = "warning"

        return {
            "turns_monitored": len(turns),
            "status": status,
            "alerts": [a.to_dict() for a in alerts],
            "avg_turn_ms": round(avg_duration, 2),
            "total_errors": total_errors,
            "total_calls": total_calls,
            "error_rate": round(total_errors / total_calls, 4) if total_calls else 0.0,
        }

    def reset(self) -> None:
        """Clear the monitoring window (e.g. for a fresh session)."""
        with self._lock:
            self._turns.clear()


__all__ = ["LoopHealthMonitor", "HealthAlert"]
