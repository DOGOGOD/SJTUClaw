"""Per-turn performance metrics for the agent loop.

Tracks timing, counts, and error signals for a single
``run_agent_turn`` invocation. The metrics are collected as the loop
runs and emitted as a structured dict at the end of the turn — useful
for performance bottleneck identification and post-hoc analysis.

Design:

- Lightweight: counters are incremented in-line, no I/O.
- Thread-safe: the parallel tool execution path can record results
  from worker threads.
- Serializable: ``to_dict()`` produces a flat JSON-compatible dict.
- Aggregatable: :class:`TurnMetricsAggregator` rolls up multiple
  turns into session-level statistics.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


def _now_ms() -> float:
    return time.time() * 1000.0


@dataclass
class TurnMetrics:
    """Performance counters for one agent turn.

    Collected by the loop as it runs; callers can read the final
    snapshot via :meth:`to_dict` after the turn completes.
    """

    session_id: str = ""
    turn_id: str = ""

    # Timing (ms)
    started_at_ms: float = field(default_factory=_now_ms)
    ended_at_ms: float = 0.0

    # LLM call tracking
    llm_calls: int = 0
    llm_total_ms: float = 0.0
    llm_errors: int = 0
    llm_retries: int = 0
    llm_truncated: int = 0  # finish_reason=length recoveries

    # Tool call tracking
    tool_calls: int = 0
    tool_errors: int = 0
    tool_parallel_batches: int = 0
    tool_cache_hits: int = 0
    tool_total_ms: float = 0.0

    # Iteration tracking
    iterations: int = 0
    max_iterations_reached: bool = False

    # Outcome
    final_status: str = ""  # "ok" | "error" | "truncated"
    error_message: str = ""

    # Context governance
    compaction_triggered: bool = False
    context_truncated: bool = False

    _llm_timer_start: float = field(default=0.0, repr=False)
    _tool_timer_start: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # Recording hooks (called by the loop)
    # ------------------------------------------------------------------

    def start_llm_call(self) -> None:
        with self._lock:
            self._llm_timer_start = _now_ms()

    def end_llm_call(self, *, ok: bool = True, truncated: bool = False) -> None:
        with self._lock:
            self.llm_calls += 1
            if self._llm_timer_start > 0:
                self.llm_total_ms += _now_ms() - self._llm_timer_start
                self._llm_timer_start = 0.0
            if not ok:
                self.llm_errors += 1
            if truncated:
                self.llm_truncated += 1

    def record_llm_retry(self) -> None:
        with self._lock:
            self.llm_retries += 1

    def start_tool_call(self) -> None:
        with self._lock:
            self._tool_timer_start = _now_ms()

    def end_tool_call(self, *, ok: bool = True, cache_hit: bool = False) -> None:
        with self._lock:
            self.tool_calls += 1
            if self._tool_timer_start > 0:
                self.tool_total_ms += _now_ms() - self._tool_timer_start
                self._tool_timer_start = 0.0
            if not ok:
                self.tool_errors += 1
            if cache_hit:
                self.tool_cache_hits += 1

    def record_parallel_batch(self, batch_size: int) -> None:
        with self._lock:
            self.tool_parallel_batches += 1
            # batch_size is tracked via tool_calls (incremented per-tool
            # by end_tool_call); we just note that a batch happened.

    def record_iteration(self) -> None:
        with self._lock:
            self.iterations += 1

    def record_max_iterations(self) -> None:
        with self._lock:
            self.max_iterations_reached = True

    def record_compaction(self) -> None:
        with self._lock:
            self.compaction_triggered = True

    def record_context_truncation(self) -> None:
        with self._lock:
            self.context_truncated = True

    def finalize(self, *, status: str = "ok", error: str = "") -> None:
        with self._lock:
            self.ended_at_ms = _now_ms()
            self.final_status = status
            self.error_message = error

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------

    @property
    def duration_ms(self) -> float:
        end = self.ended_at_ms if self.ended_at_ms > 0 else _now_ms()
        return end - self.started_at_ms

    @property
    def avg_llm_ms(self) -> float:
        return self.llm_total_ms / self.llm_calls if self.llm_calls else 0.0

    @property
    def avg_tool_ms(self) -> float:
        return self.tool_total_ms / self.tool_calls if self.tool_calls else 0.0

    @property
    def error_rate(self) -> float:
        total = self.llm_calls + self.tool_calls
        if total == 0:
            return 0.0
        return (self.llm_errors + self.tool_errors) / total

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "duration_ms": round(self.duration_ms, 2),
                "iterations": self.iterations,
                "max_iterations_reached": self.max_iterations_reached,
                "llm": {
                    "calls": self.llm_calls,
                    "total_ms": round(self.llm_total_ms, 2),
                    "avg_ms": round(self.avg_llm_ms, 2),
                    "errors": self.llm_errors,
                    "retries": self.llm_retries,
                    "truncated": self.llm_truncated,
                },
                "tools": {
                    "calls": self.tool_calls,
                    "total_ms": round(self.tool_total_ms, 2),
                    "avg_ms": round(self.avg_tool_ms, 2),
                    "errors": self.tool_errors,
                    "parallel_batches": self.tool_parallel_batches,
                    "cache_hits": self.tool_cache_hits,
                },
                "compaction_triggered": self.compaction_triggered,
                "context_truncated": self.context_truncated,
                "final_status": self.final_status,
                "error_message": self.error_message,
                "error_rate": round(self.error_rate, 4),
            }


class TurnMetricsAggregator:
    """Roll up multiple :class:`TurnMetrics` into session-level stats.

    Useful for performance bottleneck identification: callers can see
    which phase (LLM vs tools vs compaction) dominates a session's
    wall-clock time.
    """

    def __init__(self):
        self._turns: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, metrics: TurnMetrics) -> None:
        with self._lock:
            self._turns.append(metrics.to_dict())

    def summary(self) -> dict[str, Any]:
        with self._lock:
            turns = list(self._turns)

        if not turns:
            return {"turns": 0}

        total_duration = sum(t["duration_ms"] for t in turns)
        total_llm = sum(t["llm"]["total_ms"] for t in turns)
        total_tools = sum(t["tools"]["total_ms"] for t in turns)
        total_llm_calls = sum(t["llm"]["calls"] for t in turns)
        total_tool_calls = sum(t["tools"]["calls"] for t in turns)
        total_errors = sum(t["llm"]["errors"] + t["tools"]["errors"] for t in turns)
        max_iter_reached = sum(1 for t in turns if t["max_iterations_reached"])
        compactions = sum(1 for t in turns if t["compaction_triggered"])

        return {
            "turns": len(turns),
            "total_duration_ms": round(total_duration, 2),
            "llm_total_ms": round(total_llm, 2),
            "tools_total_ms": round(total_tools, 2),
            "llm_pct": round(100 * total_llm / total_duration, 1) if total_duration else 0.0,
            "tools_pct": round(100 * total_tools / total_duration, 1) if total_duration else 0.0,
            "llm_calls": total_llm_calls,
            "tool_calls": total_tool_calls,
            "errors": total_errors,
            "max_iter_reached_turns": max_iter_reached,
            "compaction_triggered_turns": compactions,
            "avg_turn_ms": round(total_duration / len(turns), 2),
        }


__all__ = ["TurnMetrics", "TurnMetricsAggregator"]
