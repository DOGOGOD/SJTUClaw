"""Download tool (Step 8): create_download.

``create_download`` registers a workspace file for download via Gateway.
It does NOT return file content to the model — only a downloadId that
the frontend can use to retrieve the file.

Gateway stores these entries in memory; they are short-lived (the file
must already exist at creation time, but the entry persists until the
server restarts or is explicitly cleaned up).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from claw.tools.base import Tool, ToolResult
from claw.workspace.manager import WorkspaceManager, WorkspaceError


# ---------------------------------------------------------------------------
# Download registry (in-memory, shared with Gateway)
# ---------------------------------------------------------------------------

_downloads: dict[str, Path] = {}
"""downloadId -> absolute Path to the workspace file."""


def register_download(file_path: Path) -> str:
    """Create a download entry and return its id."""
    download_id = f"dl_{uuid.uuid4().hex[:12]}"
    _downloads[download_id] = file_path
    return download_id


def get_download(download_id: str) -> Path | None:
    """Return the file path for *download_id* or None."""
    return _downloads.get(download_id)


def list_downloads() -> dict[str, str]:
    """Return {downloadId: file_name} for all active downloads."""
    return {did: p.name for did, p in _downloads.items()}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _make_create_download_handler(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        session_id = session_id_provider()

        try:
            resolved = workspace_manager.resolve(
                session_id, path_str, must_exist=True
            )
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not resolved.is_file():
            return ToolResult(
                ok=False,
                error=f"create_download 失败：路径不是文件 \"{path_str}\"",
            )

        try:
            download_id = register_download(resolved)
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=f"create_download 失败：{exc}",
            )

        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "tool": "create_download",
                    "path": path_str,
                    "downloadId": download_id,
                    "fileName": resolved.name,
                    "result": "下载入口已创建",
                },
                ensure_ascii=False,
            ),
        )

    return handler


# ---------------------------------------------------------------------------
# Tool definition factory
# ---------------------------------------------------------------------------


def create_download_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Tool:
    return Tool(
        name="create_download",
        description=(
            "为 workspace 内已有文件创建一个可通过 Gateway 下载的临时入口。"
            "需要提供 path 参数（相对于 workspace 的文件路径）。"
            "文件必须已存在。返回 downloadId，前端可通过此 ID 下载文件。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要创建下载入口的文件路径（相对于 workspace）",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_make_create_download_handler(workspace_manager, session_id_provider),
        safety_level="download",
    )
