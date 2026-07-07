"""Tool data structure and registry.

A ``Tool`` is a named, schema-described capability exposed to the LLM.
The model can *request* a tool call; actual execution always runs inside
``ToolRegistry.execute_by_name`` — the LLM never runs a handler directly.

Safety levels (current + reserved for future steps):

    read_only  – observes the environment without side effects (Step 5)
    write      – modifies files within the workspace boundary (Step 8)
    shell      – executes shell commands within the workspace (Step 8)
    download   – creates temporary download entries (Step 8)

All tools in Step 5 carry ``safety_level='read_only'``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolResult:
    """Unified result returned by every tool execution.

    ``ok`` is True when the tool succeeded; False when it failed.
    Exactly one of ``content`` / ``error`` is meaningful per invocation.
    """

    ok: bool
    content: str | None = None
    error: str | None = None

    def __post_init__(self):
        if self.ok and self.error is not None:
            raise ValueError("successful ToolResult must not carry error")
        if not self.ok and self.content is not None:
            raise ValueError("error ToolResult must not carry content")


@dataclass(frozen=True)
class Tool:
    """A registered tool the LLM may request to use.

    Attributes:
        name: unique tool name, e.g. ``"list_dir"``.
        description: human-readable explanation for the model.
        input_schema: JSON Schema dict describing the expected arguments.
        handler: callable ``(args: dict) -> ToolResult``.
        safety_level: one of ``read_only`` / ``write`` / ``shell`` /
            ``download``.  Only ``read_only`` is implemented in Step 5.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]
    safety_level: str = "read_only"


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
    """Validate *args* against a (subset of) JSON Schema.

    Returns a list of human-readable error strings (empty = valid).
    """
    errors: list[str] = []

    if not isinstance(args, dict):
        return ["参数必须是 JSON 对象"]

    properties: dict = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    # Check required fields
    for key in required:
        if key not in args:
            errors.append(f"缺少必需参数: \"{key}\"")
        elif args[key] is None:
            errors.append(f"必需参数 \"{key}\" 不能为 null")

    # Check each provided arg against its schema
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

        # Validate enum if present
        enum_values = prop.get("enum")
        if enum_values is not None and value is not None and value not in enum_values:
            errors.append(
                f"参数 \"{key}\" 的值 \"{value}\" 不在允许的范围内"
                f"（允许值：{enum_values}）"
            )

        # Basic string constraints
        if expected_type == "string" and isinstance(value, str):
            min_len = prop.get("minLength")
            max_len = prop.get("maxLength")
            if min_len is not None and len(value) < min_len:
                errors.append(
                    f"参数 \"{key}\" 的长度 {len(value)} 小于最小要求 {min_len}"
                )
            if max_len is not None and len(value) > max_len:
                errors.append(
                    f"参数 \"{key}\" 的长度 {len(value)} 超过最大限制 {max_len}"
                )

        # Basic number constraints
        if expected_type in ("number", "integer") and isinstance(value, (int, float)):
            minimum = prop.get("minimum")
            maximum = prop.get("maximum")
            if minimum is not None and value < minimum:
                errors.append(
                    f"参数 \"{key}\" 的值 {value} 小于最小允许值 {minimum}"
                )
            if maximum is not None and value > maximum:
                errors.append(
                    f"参数 \"{key}\" 的值 {value} 大于最大允许值 {maximum}"
                )

    return errors


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class ToolRegistryError(RuntimeError):
    """Raised when a tool lookup or execution fails at the registry level."""


class ToolRegistry:
    """Holds all registered ``Tool`` instances and dispatches execution.

    Usage::

        registry = ToolRegistry()
        registry.register(read_file_tool)
        definitions = registry.list_definitions()   # for the LLM (API-native)
        result = registry.execute_by_name("read_file", {"path": "README.md"})
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    # -- registration -------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolRegistryError(f"tool 名称冲突: {tool.name} 已注册")
        self._tools[tool.name] = tool

    # -- listing ------------------------------------------------------------

    def list_tool_names(self) -> list[str]:
        """Return the names of all registered tools."""
        return sorted(self._tools.keys())

    def list_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI-compatible ``tools`` format.

        These can be passed directly as the API ``tools`` parameter.
        """
        definitions: list[dict[str, Any]] = []
        for tool in self._tools.values():
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
            )
        return definitions

    def list_compact_definitions(self) -> list[dict[str, Any]]:
        """Return lightweight definitions for embedding in system messages.

        These are used in the JSON-protocol fallback path where the
        tool definitions must travel inline (no native *tools* parameter).
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "safety_level": tool.safety_level,
            }
            for tool in self._tools.values()
        ]

    # -- execution ----------------------------------------------------------

    def execute_by_name(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Find the tool by *name*, validate *args*, and run its handler.

        Returns:
            ``ToolResult(ok=True, content=...)`` on success, or
            ``ToolResult(ok=False, error=...)`` on failure (bad name,
            invalid args, or handler exception).

        The caller should always receive a ``ToolResult`` — this method
        never raises.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=(
                    f"未知的 tool: \"{name}\"。"
                    f"可用的 tool: {self.list_tool_names()}"
                ),
            )

        # Validate args
        validation_errors = _validate_args(args, tool.input_schema)
        if validation_errors:
            return ToolResult(
                ok=False,
                error="参数校验失败：\n" + "\n".join(
                    f"  - {e}" for e in validation_errors
                ),
            )

        # Execute handler — catch all exceptions so the agent loop never
        # crashes on a tool failure.
        try:
            return tool.handler(args)
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=f"tool \"{name}\" 执行时发生未预期的异常：{exc}",
            )

    def get_tool(self, name: str) -> Tool | None:
        """Return the registered ``Tool`` or ``None``."""
        return self._tools.get(name)
