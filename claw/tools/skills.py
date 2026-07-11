"""Skill selection tool (Step 9): ``use_skill``.

This tool is registered with safety_level ``skill_select`` — it is NOT
executed by ``ToolRegistry.execute_by_name`` in the normal flow.
Instead, the agent loop intercepts ``skill_select`` calls, creates a
skill approval for the user, and injects the skill content into the
session if approved.
"""

from __future__ import annotations

from typing import Any

from claw.tools.base import Tool, ToolResult


def _handle_use_skill(_args: dict[str, Any]) -> ToolResult:
    """Stub handler — real logic is in ``claw.agent.loop``."""
    return ToolResult(
        ok=False,
        error=(
            "use_skill 这是一个内部工具，应由 agent loop 处理，"
            "不应通过 ToolRegistry 直接执行。"
        ),
    )


def create_use_skill_tool() -> Tool:
    return Tool(
        name="use_skill",
        description=(
            "当你判断用户的任务适合使用某个 skill 时调用此工具。"
            "需要提供 skill_name（skill 名称）和 reason（为什么选择此 skill）。"
            "调用后系统会请求用户确认，确认后加载对应 skill 的完整说明。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "要使用的 skill 名称（从可用 skills 列表中选择）",
                },
                "reason": {
                    "type": "string",
                    "description": "为什么选择此 skill 来帮助完成任务",
                },
            },
            "required": ["skill_name", "reason"],
        },
        handler=_handle_use_skill,
        safety_level="skill_select",
    )
