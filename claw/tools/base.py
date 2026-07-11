"""Tool data structure and registry — v6.

Enhancements over v5:

- ``concurrency_safe`` flag for parallel tool execution.
- ``ContextAware`` interface for tools that need session/channel routing.
- ``RequestContext`` for binding session, channel, chat_id to tools.
- Tool result auto-truncation to ``max_result_chars``.
- Workspace scope integration (path traversal detection).
- SSRF guard classification (hard block, conversational recovery).
- Repeated external lookup throttling.
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass, field
from typing import Any, Callable

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
        safety_level: ``read_only`` / ``write`` / ``shell`` / ``download`` / ``skill_select``.
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
            expected = _TYPE_MAP.get(expected_type)
            if expected is not None and not isinstance(value, expected):
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
        if tool.name in self._tools:
            raise ToolRegistryError(f"tool 名称冲突: {tool.name} 已注册")
        self._tools[tool.name] = tool

    # -- listing ------------------------------------------------------------

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_tool_names(self) -> list[str]:
        return self.tool_names

    def list_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for tool in self._tools.values():
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
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
            for tool in self._tools.values()
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
        prep_error: str | None = None
        if self._prepare_call is not None:
            try:
                result = self._prepare_call(name, args)
                if isinstance(result, tuple) and len(result) == 3:
                    tool, args, prep_error = result
            except Exception:
                pass

        if prep_error:
            return ToolResult(ok=False, error=prep_error)

        if tool is None:
            return ToolResult(
                ok=False,
                error=f"未知的 tool: \"{name}\"。可用的 tool: {self.list_tool_names()}",
            )

        # Validate args
        validation_errors = _validate_args(args, tool.input_schema)
        if validation_errors:
            return ToolResult(
                ok=False,
                error="参数校验失败：\n" + "\n".join(f"  - {e}" for e in validation_errors),
            )

        # Execute
        try:
            result = tool.handler(args)
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=f"tool \"{name}\" 执行时发生未预期的异常：{exc}",
            )

        # Auto-truncate
        effective_max = max_result_chars or tool.max_result_chars
        if effective_max > 0 and result.ok and result.content and len(result.content) > effective_max:
            result = ToolResult(
                ok=True,
                content=result.content[:effective_max] + "\n...[truncated]",
            )

        return result

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)


# ---------------------------------------------------------------------------
# Tool auto-discovery helpers
# ---------------------------------------------------------------------------


def discover_tools(package_path: str = "claw.tools") -> list[Tool]:
    """Auto-discover Tool instances in a package via pkgutil.

    Scans all modules in *package_path* for ``Tool`` instances and
    functions decorated with ``@register_tool``.
    """
    import importlib
    import pkgutil

    tools: list[Tool] = []
    try:
        package = importlib.import_module(package_path)
    except ImportError:
        return tools

    for _, modname, is_pkg in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name, None)
            if isinstance(attr, Tool):
                tools.append(attr)
            elif callable(attr) and hasattr(attr, "_claw_tool"):
                tools.append(attr._claw_tool)

        if is_pkg:
            tools.extend(discover_tools(modname))

    return tools


def register_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    safety_level: str = "read_only",
    concurrency_safe: bool = False,
    max_result_chars: int = 0,
):
    """Decorator that registers a handler function as a Tool.

    Usage::

        @register_tool("search", "Search the web", {"type": "object", ...})
        def search(args):
            ...
    """
    tool = Tool(
        name=name,
        description=description,
        input_schema=input_schema,
        handler=None,  # filled in by decorator
        safety_level=safety_level,
        concurrency_safe=concurrency_safe,
        max_result_chars=max_result_chars,
    )

    def decorator(fn: Callable[[dict[str, Any]], ToolResult]):
        # Create a new Tool with the handler set
        wrapped = Tool(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            handler=fn,
            safety_level=tool.safety_level,
            concurrency_safe=tool.concurrency_safe,
            max_result_chars=tool.max_result_chars,
        )
        fn._claw_tool = wrapped
        return fn

    return decorator
