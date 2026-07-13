"""Skill tools (Hermes-agent style): ``skills_list`` + ``skill_view``.

LLM-driven progressive disclosure:
- ``skills_list`` — list available skills (name + description only, lightweight)
- ``skill_view`` — load a skill's full SKILL.md content (or a sub-file under it)

The LLM decides which skill is relevant by reading the list, then loads
its full instructions on demand.  No algorithmic matching, no approval
required — the LLM simply reads skill documents like any other file.
"""

from __future__ import annotations

import json
from typing import Any

from claw.tools.base import Tool, ToolResult


def _create_skills_list_handler(registry):
    """Handler for ``skills_list`` — return name + description JSON."""

    def handler(_args: dict[str, Any]) -> ToolResult:
        skills = registry.list_skills()
        if not skills:
            return ToolResult(
                ok=True,
                content=json.dumps(
                    {"skills": [], "count": 0, "hint": "No skills found."},
                    ensure_ascii=False,
                ),
            )

        items = [
            {"name": s.name, "description": s.description}
            for s in skills
        ]
        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "skills": items,
                    "count": len(items),
                    "hint": "Use skill_view(name) to load full instructions for a skill.",
                },
                ensure_ascii=False,
            ),
        )

    return handler


def _create_skill_view_handler(registry):
    """Handler for ``skill_view`` — return full SKILL.md content."""

    def handler(args: dict[str, Any]) -> ToolResult:
        name = (args.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="缺少必需参数: name")

        skill = registry.get_skill(name)
        if skill is None:
            available = [s.name for s in registry.list_skills()]
            return ToolResult(
                ok=False,
                error=json.dumps(
                    {
                        "error": f"未找到 skill: \"{name}\"",
                        "available_skills": available,
                        "hint": "使用 skills_list 查看所有可用 skill",
                    },
                    ensure_ascii=False,
                ),
            )
        available, reason = registry.get_skill_availability(name)
        if not available:
            return ToolResult(ok=False, error=f"Skill '{name}' 当前不可用：{reason}")

        file_path = (args.get("file_path") or "").strip()

        if file_path:
            # Sub-file access (references/, templates/, assets/)
            target = skill.directory / file_path
            try:
                target.resolve().relative_to(skill.directory.resolve())
            except ValueError:
                return ToolResult(
                    ok=False,
                    error=f"路径越界: \"{file_path}\" 不在 skill 目录内",
                )
            if not target.exists():
                return ToolResult(
                    ok=False,
                    error=f"文件不存在: \"{file_path}\"",
                )
            if not target.is_file():
                return ToolResult(
                    ok=False,
                    error=f"路径不是文件: \"{file_path}\"",
                )
            try:
                if target.stat().st_size > 256 * 1024:
                    return ToolResult(ok=False, error="Skill 子文件超过 256 KiB，请拆分后读取")
            except OSError as exc:
                return ToolResult(ok=False, error=f"无法读取文件信息: {exc}")
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return ToolResult(
                    ok=False,
                    error=f"无法读取文件: {exc}",
                )
            return ToolResult(
                ok=True,
                content=json.dumps(
                    {
                        "skill_name": name,
                        "file_path": file_path,
                        "content": content,
                    },
                    ensure_ascii=False,
                ),
            )

        # Main SKILL.md content
        full = registry.format_full_content(name)
        if len(full) > 200_000:
            return ToolResult(ok=False, error="SKILL.md 内容过长，请拆分到 references 后按需读取")

        # Build linked files index
        linked: dict[str, list[str]] = {}
        for sub in ("references", "templates", "assets"):
            subdir = skill.directory / sub
            if subdir.is_dir():
                files = [
                    str(f.relative_to(skill.directory))
                    for f in sorted(subdir.rglob("*"))
                    if f.is_file()
                ]
                if files:
                    linked[sub] = files

        result: dict[str, Any] = {
            "skill_name": name,
            "description": skill.description,
            "content": full,
        }
        if linked:
            result["linked_files"] = linked
            result["hint"] = (
                "此 skill 包含链接文件。"
                "使用 skill_view(name, file_path) 读取，"
                "例如 skill_view(name=\"course-report\", file_path=\"references/checklist.md\")"
            )

        # Record usage telemetry (best-effort)
        try:
            registry.record_view(name)
        except Exception:
            pass

        return ToolResult(
            ok=True,
            content=json.dumps(result, ensure_ascii=False),
        )

    return handler


def create_skills_list_tool(registry) -> Tool:
    """Create the ``skills_list`` tool — browse available skills."""
    return Tool(
        name="skills_list",
        description=(
            "列出所有可用的 skill（技能）。每个 skill 是一份特定任务的工作指南，"
            "包含操作流程、模板和参考资料。返回 name 和 description 的简要列表。"
            "当你需要了解有哪些任务指南可用时调用此工具。"
            "查看具体 skill 的完整内容请使用 skill_view。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_create_skills_list_handler(registry),
        safety_level="read_only",
        concurrency_safe=True,
    )


def create_skill_view_tool(registry) -> Tool:
    """Create the ``skill_view`` tool — load full skill content."""
    return Tool(
        name="skill_view",
        description=(
            "加载指定 skill 的完整说明文档（SKILL.md）或其中某个子文件。"
            "当你从 skills_list 中看到一个合适的 skill 后，调用此工具获取其完整操作流程、"
            "模板要求、检查清单等详细内容。"
            "如果 skill 包含 references/templates/assets 子文件，"
            "可通过 file_path 参数访问（例如 \"references/checklist.md\"）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称（从 skills_list 返回的列表中选择）",
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "可选：skill 目录下的子文件路径。"
                        "如 \"references/checklist.md\" 或 \"templates/report.md\"。"
                        "不填则返回 SKILL.md 主文档。"
                    ),
                },
            },
            "required": ["name"],
        },
        handler=_create_skill_view_handler(registry),
        safety_level="read_only",
        concurrency_safe=True,
    )
