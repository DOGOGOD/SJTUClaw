"""Model-output protocol: parse tool-call and final responses.

The LLM may respond in one of three ways (JSON protocol), or natively
through the API's ``tool_calls`` field.

JSON protocol (fallback for models without native function calling):

    {"type": "tool_call",  "tool": "...", "args": {...}}
    {"type": "tool_calls", "calls": [{"tool": "...", "args": {...}}, ...]}
    {"type": "final",      "content": "..."}

The parser tolerates text before/after the JSON object — it extracts
the first valid protocol JSON it finds.  String matching alone is never
sufficient to identify a tool call; we always parse the JSON structure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# A single round can include at most 5 tool calls.
MAX_TOOL_CALLS_PER_TURN = 5


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRequest:
    """A single tool invocation requested by the model."""

    name: str
    args: dict[str, Any]


@dataclass
class AgentResponse:
    """Structured result of one LLM call in the agent loop.

    Exactly one of ``final`` / ``tool_calls`` is meaningful:

    - ``final`` is set → the model is done, return the text to the user.
    - ``tool_calls`` is set → execute the requested tools and feed the
      results back into the next LLM call.
    """

    final: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)

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

    The model is allowed to emit surrounding text ("好的，让我来读取文件...")
    before or after the actual protocol JSON. This function scans for the
    outermost braces and attempts to parse the substring as JSON; it
    falls back to progressively shorter substrings if the outermost
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
            raise ProtocolParseError("tool_call 缺少有效的 \"tool\" 字段")
        if not isinstance(tool_args, dict):
            raise ProtocolParseError("tool_call 的 \"args\" 必须是对象")
        return [ToolCallRequest(name=tool_name, args=tool_args)]

    if obj_type == "tool_calls":
        calls_raw = obj.get("calls", [])
        if not isinstance(calls_raw, list):
            raise ProtocolParseError("tool_calls 的 \"calls\" 必须是一个数组")
        if len(calls_raw) > MAX_TOOL_CALLS_PER_TURN:
            raise ProtocolParseError(
                f"tool_calls 单轮调用数 {len(calls_raw)} 超过上限 {MAX_TOOL_CALLS_PER_TURN}"
            )
        calls: list[ToolCallRequest] = []
        for idx, call_item in enumerate(calls_raw):
            if not isinstance(call_item, dict):
                raise ProtocolParseError(
                    f"tool_calls[].calls[{idx}] 必须是对象"
                )
            tool_name = call_item.get("tool")
            tool_args = call_item.get("args", {})
            if not isinstance(tool_name, str) or not tool_name:
                raise ProtocolParseError(
                    f"tool_calls[].calls[{idx}] 缺少有效的 \"tool\" 字段"
                )
            if not isinstance(tool_args, dict):
                raise ProtocolParseError(
                    f"tool_calls[].calls[{idx}] 的 \"args\" 必须是对象"
                )
            calls.append(ToolCallRequest(name=tool_name, args=tool_args))
        return calls

    if obj_type == "final":
        return []

    raise ProtocolParseError(
        f"未知的协议类型 \"{obj_type}\"，期望 \"tool_call\" / \"tool_calls\" / \"final\""
    )


# ---------------------------------------------------------------------------
# Public parse entry-point
# ---------------------------------------------------------------------------


class ProtocolParseError(RuntimeError):
    """Raised when the model's output does not conform to the protocol."""


def parse_agent_response(
    content_text: str | None,
    native_tool_calls: list[dict[str, Any]] | None = None,
) -> AgentResponse:
    """Parse the LLM output into a structured ``AgentResponse``.

    Prefers native tool calls (API-level ``tool_calls``) when available.
    Otherwise falls back to parsing the JSON protocol from *content_text*.

    Args:
        content_text: the text ``content`` from the LLM message (may be
            None if the model only returned tool calls).
        native_tool_calls: raw ``tool_calls`` list from the API response
            (OpenAI format), or None.

    Returns:
        ``AgentResponse`` — either ``final`` or ``tool_calls``.

    Raises:
        ProtocolParseError: if the model output is present but cannot be
            parsed as either a tool call or a final response.
    """
    # 1. Native function calling — preferred path
    if native_tool_calls:
        calls: list[ToolCallRequest] = []
        for tc in native_tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                raise ProtocolParseError(
                    f"tool \"{name}\" 的 arguments 不是合法 JSON: {args_str!r}"
                )
            if not isinstance(args, dict):
                raise ProtocolParseError(
                    f"tool \"{name}\" 的 arguments 必须是 JSON 对象"
                )
            calls.append(ToolCallRequest(name=name, args=args))

        if len(calls) > MAX_TOOL_CALLS_PER_TURN:
            raise ProtocolParseError(
                f"单轮工具调用数 {len(calls)} 超过上限 {MAX_TOOL_CALLS_PER_TURN}"
            )
        return AgentResponse(tool_calls=calls)

    # 2. JSON-protocol fallback
    if content_text:
        obj = extract_protocol_json(content_text)
        if obj is None:
            # No JSON object found — treat as final response
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

        # obj had "type" but wasn't tool_call/tool_calls/final → treat as final
        return AgentResponse(final=content_text)

    # No content and no tool calls — empty response
    raise ProtocolParseError("模型返回了空响应")


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
        "## 可用工具 (Tools)\n\n"
        "你可以调用以下工具来获取真实的环境信息。\n"
        "当你需要调用工具时，必须严格按照以下 JSON 协议输出，"
        "不要输出额外的解释或前后缀文字。\n\n"
        f"{defs_json}\n\n"
        "### 协议格式\n\n"
        "**请求调用单个工具**（将整个 JSON 放在你的回复末尾）：\n"
        '{"type": "tool_call", "tool": "<工具名>", "args": {...}}\n\n'
        "**请求同时调用多个工具**（最多5个）：\n"
        '{"type": "tool_calls", "calls": ['
        '{"tool": "<工具名>", "args": {...}}, ...'
        "]}\n\n"
        "**输出最终回答**：\n"
        '{"type": "final", "content": "<你的完整回答>"}\n\n'
        "重要规则：\n"
        "- 每次只能选择一种格式：tool_call、tool_calls 或 final。\n"
        "- 如果需要调用工具，先输出 tool_call(s)，等待工具结果返回后再继续推理。\n"
        "- 当准备好回答用户时，输出 final 格式，content 中包含你的完整回答。\n"
        "- 工具执行结果会以 `[tool_result]` 消息的形式提供给你。\n"
        "- 如果工具返回错误，你需要基于错误信息调整策略，而不是假装成功。\n"
    )
