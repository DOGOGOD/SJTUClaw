"""Attachment tool (Step 8): copy_attachment_to_workspace.

Copies a Gateway-uploaded attachment (bound to the current session) into
the workspace. Cannot access attachments from other sessions.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

from claw.tools.base import Tool, ToolResult
from claw.workspace.manager import WorkspaceManager, WorkspaceError


def _make_copy_attachment_handler(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
    sessions_dir: Path,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        attachment_id: str = args["attachment_id"]
        dest_path_str: str = args.get("dest_path", attachment_id)
        session_id = session_id_provider()

        # Resolve the destination path within workspace
        try:
            dest_resolved = workspace_manager.resolve(session_id, dest_path_str)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        # Locate the attachment in the current session's attachment dir
        att_dir = sessions_dir / session_id / "attachments"
        meta_file = att_dir / ".meta.json"

        # Read metadata (empty list if file missing / corrupted)
        meta_list: list = []
        if meta_file.exists():
            try:
                parsed = json.loads(meta_file.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    meta_list = parsed
            except (OSError, json.JSONDecodeError):
                pass

        # Search for the requested attachment
        record = None
        for r in meta_list:
            if isinstance(r, dict) and r.get("id") == attachment_id:
                record = r
                break

        if record is None:
            # Check whether this id exists in *any* session to give a
            # more helpful error message.
            found_in_other = False
            if sessions_dir.exists():
                for entry in sessions_dir.iterdir():
                    if not entry.is_dir() or entry.name == session_id:
                        continue
                    other_meta = entry / "attachments" / ".meta.json"
                    if not other_meta.exists():
                        continue
                    try:
                        other_list = json.loads(
                            other_meta.read_text(encoding="utf-8")
                        )
                        if isinstance(other_list, list):
                            for orr in other_list:
                                if (
                                    isinstance(orr, dict)
                                    and orr.get("id") == attachment_id
                                ):
                                    found_in_other = True
                                    break
                    except Exception:
                        pass
                    if found_in_other:
                        break

            if found_in_other:
                return ToolResult(
                    ok=False,
                    error=(
                        f"附件 \"{attachment_id}\" 属于其他 session，"
                        f"不在当前 session {session_id} 中。"
                        f"copy_attachment_to_workspace 只能访问当前 session "
                        f"的附件，不能访问其他 session 的附件。"
                    ),
                )
            else:
                return ToolResult(
                    ok=False,
                    error=(
                        f"附件 \"{attachment_id}\" 未找到。"
                        f"请确认附件 ID 正确且已上传到当前 session {session_id}。"
                    ),
                )

        stored_name = record.get("storedName", "")
        src_path = att_dir / stored_name

        if not src_path.exists():
            return ToolResult(
                ok=False,
                error=f"附件文件 \"{stored_name}\" 在磁盘上不存在",
            )

        # Copy to workspace
        try:
            dest_resolved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_path), str(dest_resolved))
        except OSError as exc:
            return ToolResult(
                ok=False,
                error=f"复制附件到 workspace 失败: {exc}",
            )

        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "tool": "copy_attachment_to_workspace",
                    "attachment_id": attachment_id,
                    "original_name": record.get("originalName", ""),
                    "dest_path": dest_path_str,
                    "result": f"附件已复制到 workspace: {dest_path_str}",
                },
                ensure_ascii=False,
            ),
        )

    return handler


# ---------------------------------------------------------------------------
# Tool definition factory
# ---------------------------------------------------------------------------


def create_copy_attachment_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
    sessions_dir: Path,
) -> Tool:
    return Tool(
        name="copy_attachment_to_workspace",
        description=(
            "将当前 session 通过 Gateway 上传的附件拷贝到 workspace 内指定路径。"
            "只能访问当前 session 的附件，不能访问其他 session 的附件。"
            "需要提供 attachment_id（附件 ID）和可选的 dest_path（目标路径，"
            "默认为附件原名）。目标路径必须在 workspace 内。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": "要拷贝的附件 ID（从附件列表中获取）",
                    "minLength": 1,
                },
                "dest_path": {
                    "type": "string",
                    "description": (
                        "目标路径（相对于 workspace），默认为附件原始文件名"
                    ),
                },
            },
            "required": ["attachment_id"],
        },
        handler=_make_copy_attachment_handler(
            workspace_manager, session_id_provider, sessions_dir
        ),
        safety_level="write",
    )
