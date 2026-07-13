"""Agent turn events — emitted during tool execution for real-time streaming.

These events are consumed by SSE/WebSocket transports to provide live
tool-call visibility in the WebUI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnEvent:
    """Base class for all turn-level events."""

    timestamp: str = ""  # ISO-8601, set at emission time


@dataclass
class ThinkingEvent(TurnEvent):
    """The LLM is processing (between tool calls or before final answer)."""

    iteration: int = 0


@dataclass
class ToolCallStartEvent(TurnEvent):
    """A tool call has been dispatched for execution."""

    call_id: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    iteration: int = 0


@dataclass
class ToolCallEndEvent(TurnEvent):
    """A tool call has completed (success or failure)."""

    call_id: str = ""
    tool_name: str = ""
    ok: bool = True
    result: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class FinalEvent(TurnEvent):
    """The agent turn has completed with a final reply."""

    content: str = ""


@dataclass
class ErrorEvent(TurnEvent):
    """A non-fatal error occurred during the turn."""

    error: str = ""
