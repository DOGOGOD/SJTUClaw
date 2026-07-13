"""Tool data structure and registry — v6.

Enhancements over v5:

- ``concurrency_safe`` flag for parallel tool execution.
- ``ContextAware`` interface for tools that need session/channel routing.
- ``RequestContext`` for binding session, channel, chat_id to tools.
- Tool result auto-truncation to ``max_result_chars``.
- Workspace scope integration (path traversal detection).
- SSRF guard classification (hard block, conversational recovery).
- Repeated external lookup throttling.
- Schema normalization for OpenAI-compatible tool definitions.
- Structured tool result wrapping (JSON envelope for error recovery).
- Pre-execution guardrails (rate limiting, safety validation).
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context variables (bound per-agent-turn)
# ---------------------------------------------------------------------------

_current_request_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "tool_request_context", default=None
)
_current_file_states: contextvars.ContextVar = contextvars.ContextVar(
    "tool_file_states", default=None
)
_current_workspace_scope: contextvars.ContextVar = contextvars.ContextVar(
    "tool_workspace_scope", default=None
)


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


@dataclass
class RequestContext:
    """Per-turn context bound for ContextAware tools.

    Tools receive this via ``set_context()`` and can use it for
    channel-specific behavior, session-keyed caching, etc.
    """

    channel: str = ""
    chat_id: str = ""
    message_id: str | None = None
    session_key: str = ""
    metadata: dict = field(default_factory=dict)


def bind_request_context(ctx: RequestContext):
    """Bind *ctx* to the current async context. Returns a reset token."""
    token = _current_request_ctx.set(ctx)
    return token


def reset_request_context(token) -> None:
    _current_request_ctx.reset(token)


def get_request_context() -> RequestContext | None:
    return _current_request_ctx.get(None)


# ---------------------------------------------------------------------------
# File state tracking
# ---------------------------------------------------------------------------


class FileStates:
    """Tracks which files a tool has read or written during a turn.

    Used for read-after-write consistency: if a tool writes a file and
    another tool reads it without re-reading, the stale read can be
    detected.
    """

    def __init__(self):
        self._reads: dict[str, float] = {}   # path -> mtime at read time
        self._writes: set[str] = set()       # paths written this turn

    def record_read(self, path: str, mtime: float) -> None:
        self._reads[path] = mtime

    def record_write(self, path: str) -> None:
        self._writes.add(path)
        # Invalidate any previous read of this file
        self._reads.pop(path, None)

    def is_stale(self, path: str) -> bool:
        """Return True if the file was written after it was last read."""
        return path in self._writes and path not in self._reads

    def merge(self, other: "FileStates") -> None:
        self._reads.update(other._reads)
        self._writes.update(other._writes)


def bind_file_states(states: FileStates):
    token = _current_file_states.set(states)
    return token


def reset_file_states(token) -> None:
    _current_file_states.reset(token)


def get_file_states() -> FileStates | None:
    return _current_file_states.get(None)


# ---------------------------------------------------------------------------
# Workspace scope
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceScope:
    """Resolved workspace scope for a single turn/message.

    ``project_path``: the effective workspace root.
    ``restrict_to_workspace``: whether tools should enforce the boundary.
    ``sandbox_enabled``: whether an external sandbox is active.
    """

    project_path: str = ""
    restrict_to_workspace: bool = False
    sandbox_enabled: bool = False


def bind_workspace_scope(scope: WorkspaceScope):
    token = _current_workspace_scope.set(scope)
    return token


def reset_workspace_scope(token) -> None:
    _current_workspace_scope.reset(token)


def get_workspace_scope() -> WorkspaceScope | None:
    return _current_workspace_scope.get(None)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Unified result returned by every tool execution."""

    ok: bool
    content: str | None = None
    error: str | None = None

    def __post_init__(self):
        if self.ok and self.error is not None:
            raise ValueError("successful ToolResult must not carry error")
        if not self.ok and self.content is not None:
            raise ValueError("error ToolResult must not carry content")


class ContextAware:
    """Interface for tools that need per-turn routing context."""

    def set_context(self, ctx: RequestContext) -> None:
        pass


@dataclass
class Tool:
    """A registered tool the LLM may request to use.

    Attributes:
        name: unique tool name.
        description: human-readable explanation for the model.
        input_schema: JSON Schema dict.
        handler: callable ``(args: dict) -> ToolResult``.
        safety_level: ``read_only`` / ``write`` / ``shell`` / ``download``.
        concurrency_safe: if True, this tool can be executed in parallel with others.
        max_result_chars: auto-truncate results longer than this (0 = no limit).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]
    safety_level: str = "read_only"
    concurrency_safe: bool = False
    max_result_chars: int = 0


# ---------------------------------------------------------------------------
# Parameter validation (lightweight JSON Schema subset)
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, type | tuple] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _matches_json_type(value: Any, expected_type: str) -> bool:
    """Match JSON types without treating bool as an integer in Python."""
    if expected_type in {"integer", "number"} and isinstance(value, bool):
        return False
    expected = _TYPE_MAP.get(expected_type)
    return expected is None or isinstance(value, expected)


def _validate_args(args: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate *args* against a (subset of) JSON Schema."""
    errors: list[str] = []
    if not isinstance(args, dict):
        return ["参数必须是 JSON 对象"]
    properties: dict = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    for key in required:
        if key not in args:
            errors.append(f"缺少必需参数: \"{key}\"")
        elif args[key] is None:
            errors.append(f"必需参数 \"{key}\" 不能为 null")

    for key, value in args.items():
        prop = properties.get(key)
        if prop is None:
            errors.append(f"未知参数: \"{key}\"")
            continue
        expected_type = prop.get("type")
        if expected_type is not None and value is not None:
            if not _matches_json_type(value, expected_type):
                errors.append(
                    f"参数 \"{key}\" 的类型错误：期望 {expected_type}，"
                    f"实际为 {type(value).__name__}"
                )
                continue
        enum_values = prop.get("enum")
        if enum_values is not None and value is not None and value not in enum_values:
            errors.append(
                f"参数 \"{key}\" 的值 \"{value}\" 不在允许的范围内"
                f"（允许值：{enum_values}）"
            )
        if expected_type == "string" and isinstance(value, str):
            min_len = prop.get("minLength")
            max_len = prop.get("maxLength")
            if min_len is not None and len(value) < min_len:
                errors.append(f"参数 \"{key}\" 的长度 {len(value)} 小于最小要求 {min_len}")
            if max_len is not None and len(value) > max_len:
                errors.append(f"参数 \"{key}\" 的长度 {len(value)} 超过最大限制 {max_len}")
        if expected_type in ("number", "integer") and isinstance(value, (int, float)):
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if minimum is not None and value < minimum:
                errors.append(f"参数 \"{key}\" 的值 {value} 小于最小允许值 {minimum}")
            if maximum is not None and value > maximum:
                errors.append(f"参数 \"{key}\" 的值 {value} 大于最大允许值 {maximum}")
        if expected_type == "array" and isinstance(value, list):
            min_items = prop.get("minItems")
            max_items = prop.get("maxItems")
            if min_items is not None and len(value) < min_items:
                errors.append(f"参数 \"{key}\" 的元素数量小于最小要求 {min_items}")
            if max_items is not None and len(value) > max_items:
                errors.append(f"参数 \"{key}\" 的元素数量超过最大限制 {max_items}")
            item_schema = prop.get("items")
            if isinstance(item_schema, dict) and isinstance(item_schema.get("type"), str):
                for index, item in enumerate(value):
                    if not _matches_json_type(item, item_schema["type"]):
                        errors.append(
                            f"参数 \"{key}[{index}]\" 的类型错误：期望 {item_schema['type']}，"
                            f"实际为 {type(item).__name__}"
                        )

    return errors


def is_tool_error_result(name: str, result: Any) -> bool:
    """Heuristic: does *result* look like a tool-level error string?"""
    if not isinstance(result, str):
        return False
    lower = result.lower()
    return any(marker in lower for marker in (
        "error:", "exception:", "traceback", "permission denied",
        "command not found", "cannot find",
    ))


# ---------------------------------------------------------------------------
# SSRF / Workspace boundary classification
# ---------------------------------------------------------------------------

_SSRF_MARKERS = (
    "internal/private url detected",
    "private/internal address",
    "private address",
    "ssrf",
)
_WORKSPACE_VIOLATION_MARKERS = (
    "outside the configured workspace",
    "outside allowed directory",
    "path traversal detected",
    "路径越界",
    "不在 workspace",
)

_SSRF_BOUNDARY_NOTE = (
    "这是一个不可绕过的安全边界。停止尝试访问私有/内部 URL。"
    "不要使用 curl、wget、编码 IP、替代 DNS、重定向或代理来重试。"
    "请用户提供本地文件、日志、截图或明确的公共 URL。"
)


def is_ssrf_violation(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SSRF_MARKERS)


def is_workspace_violation(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _WORKSPACE_VIOLATION_MARKERS)


def classify_boundary_error(
    tool_name: str,
    error_text: str,
    *,
    violation_counts: dict[str, int] | None = None,
) -> tuple[str | None, bool]:
    """Classify a tool error as a safety boundary violation.

    Returns ``(escalated_message, is_ssrf)`` or ``(None, False)`` if
    the error is not a boundary violation.
    """
    if is_ssrf_violation(error_text):
        return (
            f"{error_text.strip()}\n\n{_SSRF_BOUNDARY_NOTE}",
            True,
        )

    if is_workspace_violation(error_text):
        if violation_counts is not None:
            key = tool_name
            violation_counts[key] = violation_counts.get(key, 0) + 1
            if violation_counts[key] >= 3:
                return (
                    f"你已经多次尝试访问 workspace 外部的路径。"
                    f"这是不可绕过的安全边界。请用户将需要的文件复制到 workspace 内，"
                    f"或使用 /workspace set 切换到正确的目录。\n\n"
                    f"原始错误: {error_text}",
                    False,
                )
        return (error_text, False)

    return (None, False)


# ---------------------------------------------------------------------------
# Schema normalization
# ---------------------------------------------------------------------------


def normalize_tool_schema(schema: Any) -> dict[str, Any] | None:
    """Return a function-tool dict with a resolvable top-level ``name``.

    Handles both bare function schemas (``{"name": ..., "parameters": ...}``)
    and already-wrapped OpenAI tool entries (``{"type": "function",
    "function": {...}}``).  Returns ``None`` for schemas without a
    resolvable name, so callers can skip-with-warning rather than
    poisoning the LLM request with a nameless tool.
    """
    if not isinstance(schema, dict):
        return None
    # Unwrap an already-wrapped OpenAI tool entry
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        schema = schema["function"]
        if not isinstance(schema, dict):
            return None
    name = schema.get("name", "")
    if not name or not isinstance(name, str):
        return None
    return schema


def standardize_tool_result(
    tool_name: str,
    result: ToolResult,
) -> str:
    """Wrap a ToolResult into a structured JSON envelope.

    This gives the LLM a consistent format to parse:

    - Success: ``{"tool": "read_file", "ok": true, "result": "..."}``
    - Failure: ``{"tool": "read_file", "ok": false, "result": "error: ..."}``

    The ``result`` key always carries the human-readable content or
    error message, so the model can extract it uniformly.
    """
    if result.ok:
        return json.dumps(
            {
                "tool": tool_name,
                "ok": True,
                "result": result.content or "(空结果)",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "tool": tool_name,
            "ok": False,
            "result": f"错误: {result.error}",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Pre-execution guardrails
# ---------------------------------------------------------------------------


class ToolGuardrails:
    """Pre-execution safety checks for tool calls.

    Tracks per-tool invocation counts within a single agent turn to
    prevent runaway loops.  Integrated into ``ToolRegistry.execute_by_name``
    via the ``prepare_call`` hook or called directly.
    """

    def __init__(
        self,
        *,
        max_calls_per_tool: int = 50,
        max_total_calls: int = 200,
    ):
        self._max_per_tool = max_calls_per_tool
        self._max_total = max_total_calls
        self._tool_counts: dict[str, int] = {}
        self._total_calls = 0

    def reset(self) -> None:
        """Reset counters at the start of a new agent turn."""
        self._tool_counts.clear()
        self._total_calls = 0

    def check(self, tool_name: str) -> str | None:
        """Return an error message if the call should be blocked, else None."""
        self._total_calls += 1
        if self._total_calls > self._max_total:
            return (
                f"已达到本轮工具调用总数上限（{self._max_total}）。"
                f"请总结当前进展并回复用户。"
            )
        count = self._tool_counts.get(tool_name, 0) + 1
        self._tool_counts[tool_name] = count
        if count > self._max_per_tool:
            return (
                f"工具 \"{tool_name}\" 在本轮中已被调用 {count} 次，"
                f"超过单工具上限（{self._max_per_tool}）。"
                f"请尝试其他方法或总结当前结果。"
            )
        return None


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistryError(RuntimeError):
    """Raised when a tool lookup or execution fails at the registry level."""


class ToolRegistry:
    """Holds all registered ``Tool`` instances and dispatches execution.

    v6 enhancements:
    - ``set_context()`` propagates ``RequestContext`` to ContextAware tools.
    - ``prepare_call()`` hook for pre-execution validation.
    - Tool result auto-truncation via ``max_result_chars``.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._prepare_call: Callable | None = None

    # -- registration -------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise ToolRegistryError("只能注册 Tool 实例")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", tool.name or ""):
            raise ToolRegistryError(f"tool 名称无效: {tool.name!r}")
        if not callable(tool.handler):
            raise ToolRegistryError(f"tool {tool.name} 的 handler 不可调用")
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise ToolRegistryError(f"tool {tool.name} 的 input_schema 必须是 object schema")
        if not isinstance(tool.input_schema.get("properties", {}), dict):
            raise ToolRegistryError(f"tool {tool.name} 的 schema.properties 必须是对象")
        if tool.name in self._tools:
            raise ToolRegistryError(f"tool 名称冲突: {tool.name} 已注册")
        self._tools[tool.name] = tool

    def clear(self) -> None:
        """Remove all registered tools."""
        self._tools.clear()

    # -- listing ------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_tool_names(self) -> list[str]:
        return self.tool_names

    def list_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for name in self.tool_names:
            tool = self._tools[name]
            raw = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            # Normalize to catch schema issues before sending to LLM
            normalized = normalize_tool_schema(raw)
            if normalized is not None:
                definitions.append({
                    "type": "function",
                    "function": normalized,
                })
        return definitions

    def list_compact_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "safety_level": tool.safety_level,
            }
            for tool in (self._tools[name] for name in self.tool_names)
        ]

    # -- prepare_call hook --------------------------------------------------

    def set_prepare_call(self, fn: Callable | None) -> None:
        """Set a pre-execution hook: ``fn(name, args) -> (tool, args, error)``."""
        self._prepare_call = fn

    # -- context propagation ------------------------------------------------

    def set_context(self, ctx: RequestContext) -> None:
        for tool in self._tools.values():
            if isinstance(tool, ContextAware):
                tool.set_context(ctx)

    # -- execution ----------------------------------------------------------

    def execute_by_name(
        self,
        name: str,
        args: dict[str, Any],
        *,
        max_result_chars: int = 0,
    ) -> ToolResult:
        """Find the tool by *name*, validate *args*, and run its handler.

        Returns ``ToolResult`` — this method never raises.
        """
        # prepare_call hook (pre-execution validation, workspace resolution)
        tool = self._tools.get(name)
        # Hooks must not be able to mutate the caller's argument dictionary.
        call_args = dict(args) if isinstance(args, dict) else args
        prep_error: str | None = None
        if self._prepare_call is not None:
            try:
                result = self._prepare_call(name, call_args)
                if isinstance(result, tuple) and len(result) == 3:
                    tool, call_args, prep_error = result
                elif result is not None:
                    return ToolResult(ok=False, error="tool 预处理器返回了无效结果")
            except Exception as exc:
                # The hook may enforce workspace or permission policy.  A
                # broken guard must fail closed instead of bypassing checks.
                logger.exception(
                    "prepare_call hook 对工具 %s 抛出异常: %s", name, exc
                )
                return ToolResult(
                    ok=False,
                    error=f"tool \"{name}\" 的执行前检查失败，操作已安全中止：{exc}",
                )

        if prep_error:
            return ToolResult(ok=False, error=prep_error)

        if tool is None:
            return ToolResult(
                ok=False,
                error=f"未知的 tool: \"{name}\"。可用的 tool: {self.list_tool_names()}",
            )

        # Validate args
        validation_errors = _validate_args(call_args, tool.input_schema)
        if validation_errors:
            return ToolResult(
                ok=False,
                error="参数校验失败：\n" + "\n".join(f"  - {e}" for e in validation_errors),
            )

        # Execute
        try:
            result = tool.handler(call_args)
        except Exception as exc:
            logger.exception("tool %s 执行异常", name)
            return ToolResult(
                ok=False,
                error=f"tool \"{name}\" 执行时发生未预期的异常：{exc}",
            )

        if not isinstance(result, ToolResult):
            logger.error("tool %s 返回了无效类型: %s", name, type(result).__name__)
            return ToolResult(
                ok=False,
                error=f"tool \"{name}\" 返回格式无效（期望 ToolResult）",
            )
        if result.ok and result.content is not None and not isinstance(result.content, str):
            return ToolResult(ok=False, error=f"tool \"{name}\" 返回的 content 必须是字符串")
        if not result.ok and (not isinstance(result.error, str) or not result.error.strip()):
            return ToolResult(ok=False, error=f"tool \"{name}\" 执行失败，但未返回有效错误信息")

        # Auto-truncate — applies to both success content and error text
        # to prevent context bloat from verbose tracebacks.
        effective_max = max_result_chars or tool.max_result_chars
        if effective_max > 0:
            if result.ok and result.content and len(result.content) > effective_max:
                result = ToolResult(
                    ok=True,
                    content=result.content[:effective_max] + "\n...[truncated]",
                )
            elif not result.ok and result.error and len(result.error) > effective_max:
                result = ToolResult(
                    ok=False,
                    error=result.error[:effective_max] + "\n...[truncated]",
                )

        return result

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)
