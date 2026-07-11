"""Update tools (Step 8): create, overwrite and edit files inside the workspace.

Every tool receives a ``workspace_manager`` callable via closure so
that the workspace binding can be resolved at execution time (the
manager is set up on the shared runtime, not baked into the tool def).

All tools return a ``ToolResult`` with contextual information:

    ok        – bool
    content   – on success: JSON string with {tool, path, result}
    error     – on failure: human-readable message
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from claw.tools.base import Tool, ToolResult
from claw.workspace.manager import WorkspaceManager, WorkspaceError


def _make_update_handler(
    operation: str,
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Callable[[dict[str, Any]], ToolResult]:
    """Factory that returns a handler for create / overwrite / edit.

    *operation* is one of ``create``, ``overwrite``, ``edit``.
    """

    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        session_id = session_id_provider()

        try:
            resolved = workspace_manager.resolve(session_id, path_str)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        if operation == "create":
            return _do_create(resolved, path_str)
        elif operation == "overwrite":
            content = args.get("content", "")
            return _do_overwrite(resolved, path_str, content)
        elif operation == "edit":
            old_str: str = args.get("old_string", "")
            new_str: str = args.get("new_string", "")
            return _do_edit(resolved, path_str, old_str, new_str)
        else:
            return ToolResult(ok=False, error=f"未知操作: {operation}")

    return handler


# ---------------------------------------------------------------------------
# Low-level file operations
# ---------------------------------------------------------------------------


def _do_create(resolved: Path, display_path: str) -> ToolResult:
    """Create an empty file at *resolved*. Fails if it already exists."""
    if resolved.exists():
        return ToolResult(
            ok=False,
            error=f"create_file 失败：文件已存在 \"{display_path}\"",
        )
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("", encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=f"create_file 失败 \"{display_path}\": {exc}",
        )

    return ToolResult(
        ok=True,
        content=json.dumps(
            {"tool": "create_file", "path": display_path, "result": "文件创建成功"},
            ensure_ascii=False,
        ),
    )


def _do_overwrite(resolved: Path, display_path: str, content: str) -> ToolResult:
    """Overwrite *resolved* with *content*. Creates parent dirs as needed."""
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=f"overwrite_file 失败 \"{display_path}\": {exc}",
        )

    return ToolResult(
        ok=True,
        content=json.dumps(
            {
                "tool": "overwrite_file",
                "path": display_path,
                "result": f"文件已覆盖写入，共 {len(content)} 字符",
            },
            ensure_ascii=False,
        ),
    )


def _do_edit(
    resolved: Path, display_path: str, old_str: str, new_str: str
) -> ToolResult:
    """Replace *old_str* with *new_str* in *resolved*.

    Fails if the file doesn't exist, or if *old_str* is not found /
    appears more than once.
    """
    if not resolved.exists():
        return ToolResult(
            ok=False,
            error=f"edit_file 失败：文件不存在 \"{display_path}\"",
        )
    try:
        original = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=f"edit_file 失败 \"{display_path}\": {exc}",
        )

    count = original.count(old_str)
    if count == 0:
        return ToolResult(
            ok=False,
            error=f"edit_file 失败 \"{display_path}\": 未找到要替换的内容",
        )
    if count > 1:
        return ToolResult(
            ok=False,
            error=(
                f"edit_file 失败 \"{display_path}\": "
                f"要替换的内容出现了 {count} 次，"
                f"请提供更精确的匹配字符串以确保唯一匹配"
            ),
        )

    new_content = original.replace(old_str, new_str, 1)
    try:
        resolved.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=f"edit_file 失败 \"{display_path}\": {exc}",
        )

    return ToolResult(
        ok=True,
        content=json.dumps(
            {
                "tool": "edit_file",
                "path": display_path,
                "result": "文件已编辑，替换 1 处",
            },
            ensure_ascii=False,
        ),
    )


# ---------------------------------------------------------------------------
# Tool definition factories
# ---------------------------------------------------------------------------


def create_create_file_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Tool:
    return Tool(
        name="create_file",
        description=(
            "在 workspace 内创建一个新文件。需要提供 path 参数（字符串，"
            "相对于 workspace 的路径）。文件已存在时会失败。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要在 workspace 内创建的文件路径（相对路径）",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_make_update_handler("create", workspace_manager, session_id_provider),
        safety_level="write",
    )


def create_overwrite_file_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Tool:
    return Tool(
        name="overwrite_file",
        description=(
            "用新内容覆盖 workspace 内已有文件（或创建新文件）。"
            "需要提供 path（文件路径）和 content（新内容）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要覆盖的文件路径（相对于 workspace）",
                    "minLength": 1,
                },
                "content": {
                    "type": "string",
                    "description": "要写入文件的新内容",
                },
            },
            "required": ["path", "content"],
        },
        handler=_make_update_handler(
            "overwrite", workspace_manager, session_id_provider
        ),
        safety_level="write",
    )


def create_edit_file_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Tool:
    return Tool(
        name="edit_file",
        description=(
            "通过精确字符串替换来编辑 workspace 内的文件。"
            "old_string 必须唯一匹配文件中的一处内容，"
            "new_string 是替换后的内容。"
            "需要提供 path、old_string 和 new_string。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要编辑的文件路径（相对于 workspace）",
                    "minLength": 1,
                },
                "old_string": {
                    "type": "string",
                    "description": "要替换的原内容（必须唯一匹配）",
                    "minLength": 1,
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新内容",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_make_update_handler("edit", workspace_manager, session_id_provider),
        safety_level="write",
    )

