"""Model-output protocol: parse tool-call and final responses.

The LLM may respond in one of three ways (JSON protocol), or natively
through the API's ``tool_calls`` field.

JSON protocol (fallback for models without native function calling):

    {"type": "tool_call",  "tool": "...", "args": {...}}
    {"type": "tool_calls", "calls": [{"tool": "...", "args": {...}}, ...]}
    {"type": "final",      "content": "..."}

The parser tolerates text before/after the JSON object -- it extracts
the first valid protocol JSON it finds.  String matching alone is never
sufficient to identify a tool call; we always parse the JSON structure.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Maximum tool calls per single LLM turn.  Configurable via the
# ``CLAW_MAX_TOOL_CALLS_PER_TURN`` environment variable.
#
# Modern LLMs (GPT-4, Claude, etc.) can reasonably request 10-20 parallel
# read-only calls in one turn (e.g. exploring a project structure by
# listing multiple directories at once).  The previous hard-coded value
# of 5 was too low for such tasks and caused ProtocolParseError, which
# in turn made the agent loop retry with the same over-budget request.
#
# When the limit is exceeded the parser now **truncates** to the first
# ``MAX_TOOL_CALLS_PER_TURN`` calls (with a warning) instead of raising
# an error, so the agent can continue making progress.
MAX_TOOL_CALLS_PER_TURN = int(os.getenv("CLAW_MAX_TOOL_CALLS_PER_TURN", "20"))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRequest:
    """A single tool invocation requested by the model."""

    name: str
    args: dict[str, Any]
    call_id: str = ""  # native function-calling id for tool result matching


@dataclass
class AgentResponse:
    """Structured result of one LLM call in the agent loop.

    Exactly one of ``final`` / ``tool_calls`` is meaningful:

    - ``final`` is set -- the model is done, return the text to the user.
    - ``tool_calls`` is set -- execute the requested tools and feed the
      results back into the next LLM call.
    """

    final: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    # API-level finish reason: "stop", "length", "tool_calls", etc.
    # Used by the agent loop for length-truncation recovery.
    finish_reason: str | None = None

    @property
    def is_final(self) -> bool:
        return self.final is not None

    @property
    def is_tool_call(self) -> bool:
        return len(self.tool_calls) > 0


# ---------------------------------------------------------------------------
# JSON protocol extraction
# ---------------------------------------------------------------------------


def extract_protocol_json(text: str) -> dict[str, Any] | None:
    """Try to find and parse a protocol JSON object inside *text*.

    The model is allowed to emit surrounding text (e.g. "OK, let me read
    the file...") before or after the actual protocol JSON. This function
    scans for the outermost braces and attempts to parse the substring as
    JSON; it falls back to progressively shorter substrings if the outermost
    attempt fails.

    Returns the parsed dict on success, or None if no valid protocol
    object could be extracted.
    """
    if not text:
        return None

    # Strategy 1: find the first '{' and last '}', try that span first
    start = text.find("{")
    if start == -1:
        return None

    end = text.rfind("}")
    if end <= start:
        return None

    # Try from the outermost braces, shrinking inward
    for close_idx in range(end + 1, start, -1):
        if text[close_idx - 1 : close_idx] != "}":
            continue
        candidate = text[start:close_idx]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "type" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            continue

    # Strategy 2: find any valid JSON object in the text (brace-balanced)
    return _extract_first_json_object(text)


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Walk through *text* and return the first balanced ``{...}`` JSON object."""
    for match in re.finditer(r"\{", text):
        depth = 0
        i = match.start()
        end_idx = None
        for j in range(i, len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = j + 1
                    break
        if end_idx is not None:
            candidate = text[i:end_idx]
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and "type" in obj:
                    return obj
            except (json.JSONDecodeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# JSON protocol parser
# ---------------------------------------------------------------------------


def _parse_tool_calls_from_json(obj: dict[str, Any]) -> list[ToolCallRequest]:
    """Parse ``tool_call`` / ``tool_calls`` from a protocol JSON object.

    Returns an empty list if *obj* is a ``final`` response.
    """
    obj_type = obj.get("type")

    if obj_type == "tool_call":
        tool_name = obj.get("tool")
        tool_args = obj.get("args", {})
        if not isinstance(tool_name, str) or not tool_name:
            raise ProtocolParseError('tool_call missing valid "tool" field')
        if not isinstance(tool_args, dict):
            raise ProtocolParseError('tool_call "args" must be an object')
        return [ToolCallRequest(name=tool_name, args=tool_args)]

    if obj_type == "tool_calls":
        calls_raw = obj.get("calls", [])
        if not isinstance(calls_raw, list):
            raise ProtocolParseError('tool_calls "calls" must be an array')
        if len(calls_raw) > MAX_TOOL_CALLS_PER_TURN:
            logger.warning(
                "tool_calls count %d exceeds limit %d, truncating to %d",
                len(calls_raw), MAX_TOOL_CALLS_PER_TURN, MAX_TOOL_CALLS_PER_TURN,
            )
            calls_raw = calls_raw[:MAX_TOOL_CALLS_PER_TURN]
        calls: list[ToolCallRequest] = []
        for idx, call_item in enumerate(calls_raw):
            if not isinstance(call_item, dict):
                raise ProtocolParseError(
                    f"tool_calls[].calls[{idx}] must be an object"
                )
            tool_name = call_item.get("tool")
            tool_args = call_item.get("args", {})
            if not isinstance(tool_name, str) or not tool_name:
                raise ProtocolParseError(
                    f'tool_calls[].calls[{idx}] missing valid "tool" field'
                )
            if not isinstance(tool_args, dict):
                raise ProtocolParseError(
                    f'tool_calls[].calls[{idx}] "args" must be an object'
                )
            calls.append(ToolCallRequest(name=tool_name, args=tool_args))
        return calls

    if obj_type == "final":
        return []

    raise ProtocolParseError(
        f'Unknown protocol type "{obj_type}", '
        f'expecting "tool_call" / "tool_calls" / "final"'
    )


# ---------------------------------------------------------------------------
# Public parse entry-point
# ---------------------------------------------------------------------------


class ProtocolParseError(RuntimeError):
    """Raised when the model's output does not conform to the protocol."""


def parse_agent_response(
    content_text: str | None,
    native_tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
) -> AgentResponse:
    """Parse the LLM output into a structured ``AgentResponse``.

    Prefers native tool calls (API-level ``tool_calls``) when available.
    Otherwise falls back to parsing the JSON protocol from *content_text*.

    Args:
        content_text: the text ``content`` from the LLM message (may be
            None if the model only returned tool calls, or ``""`` if the
            model returned an empty string).
        native_tool_calls: raw ``tool_calls`` list from the API response
            (OpenAI format), or None.
        finish_reason: optional API-level finish reason (e.g. "stop",
            "length", "tool_calls").  Used to interpret empty content:
            an empty string with ``finish_reason == "stop"`` is a valid
            final response, not an error.

    Returns:
        ``AgentResponse`` -- either ``final`` or ``tool_calls``.

    Raises:
        ProtocolParseError: if the model output is present but cannot be
            parsed as either a tool call or a final response.
    """
    # 1. Native function calling -- preferred path
    if native_tool_calls:
        calls: list[ToolCallRequest] = []
        for tc in native_tool_calls:
            call_id = tc.get("id", "")
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                raise ProtocolParseError(
                    f'tool "{name}" arguments is not valid JSON: {args_str!r}'
                )
            if not isinstance(args, dict):
                raise ProtocolParseError(
                    f'tool "{name}" arguments must be a JSON object'
                )
            calls.append(ToolCallRequest(name=name, args=args, call_id=str(call_id)))

        if len(calls) > MAX_TOOL_CALLS_PER_TURN:
            logger.warning(
                "single-turn tool call count %d exceeds limit %d, truncating to %d",
                len(calls), MAX_TOOL_CALLS_PER_TURN, MAX_TOOL_CALLS_PER_TURN,
            )
            calls = calls[:MAX_TOOL_CALLS_PER_TURN]
        return AgentResponse(tool_calls=calls)

    # 2. JSON-protocol fallback
    if content_text:
        obj = extract_protocol_json(content_text)
        if obj is None:
            # No JSON object found -- treat as final response
            return AgentResponse(final=content_text)

        obj_type = obj.get("type")
        if obj_type == "final":
            content = obj.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            return AgentResponse(final=content)

        tool_calls_list = _parse_tool_calls_from_json(obj)
        if tool_calls_list:
            return AgentResponse(tool_calls=tool_calls_list)

        # obj had "type" but wasn't tool_call/tool_calls/final -- treat as final
        return AgentResponse(final=content_text)

    # 3. Empty string content — legitimate final response when the model
    #    has nothing more to say (finish_reason is typically "stop").
    if content_text == "":
        logger.debug("LLM returned empty content with finish_reason=%s; treating as final", finish_reason)
        return AgentResponse(final="")

    # No content and no tool calls — empty response
    raise ProtocolParseError("Model returned an empty response")


# ---------------------------------------------------------------------------
# Protocol instructions (for JSON fallback context)
# ---------------------------------------------------------------------------


def build_protocol_instructions(tool_defs: list[dict[str, Any]]) -> str:
    """Build a system-message block describing available tools and the
    JSON output protocol, for models that do not support native function
    calling.

    *tool_defs* should come from ``ToolRegistry.list_compact_definitions()``.
    """
    defs_json = json.dumps(tool_defs, ensure_ascii=False, indent=2)

    return (
        "## Available Tools\n\n"
        "You can call the following tools to get real information.\n"
        "When you need to call a tool, output strictly in the following JSON format.\n"
        "Do NOT output any extra explanation or trailing text.\n\n"
        f"{defs_json}\n\n"
        "### Protocol Format\n\n"
        "**Request a single tool call** -- place the entire JSON at the end of your response:\n"
        '{"type": "tool_call", "tool": "<tool_name>", "args": {...}}\n\n'
        f"**Request multiple tool calls simultaneously** -- up to {MAX_TOOL_CALLS_PER_TURN}:\n"
        '{"type": "tool_calls", "calls": ['
        '{"tool": "<tool_name>", "args": {...}}, ...'
        "]}\n\n"
        "**Output final answer**:\n"
        '{"type": "final", "content": "<your full answer>"}\n\n'
        "Important rules:\n"
        "- Each turn, choose one format: tool_call, tool_calls, or final.\n"
        "- If you need to call tools, output tool_call(s) first, then wait for results.\n"
        "- When ready to answer the user, output final format with your complete answer.\n"
        "- Tool execution results will be provided to you as [tool_result] messages.\n"
        "- If a tool returns an error, adjust your strategy based on the error.\n"
        f"- A single turn calls at most {MAX_TOOL_CALLS_PER_TURN} tools. "
        "If the task needs more steps, complete them across multiple turns.\n"
    )
