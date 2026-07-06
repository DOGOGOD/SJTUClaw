"""Read-only tools (Step 5).

All tools in this module observe the environment without side effects:

    current_time  – return the current UTC time in ISO-8601 format
    list_dir      – list files and subdirectories in a directory
    read_file     – read the contents of a text file

Every tool returns a ``ToolResult``; failures (missing file, invalid
path, oversized content, …) are reported as ``ok=False`` with a clear
error message — the agent loop never crashes on a tool failure.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claw.tools.base import Tool, ToolResult

# Maximum file size before truncation: 64 KiB of UTF-8 text.
# Larger files are truncated with a clear marker in the returned content
# so the model knows the result is incomplete.
_MAX_FILE_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_current_time(_args: dict[str, Any]) -> ToolResult:
    """Return the current UTC time."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return ToolResult(ok=True, content=now)


def _handle_list_dir(args: dict[str, Any]) -> ToolResult:
    """List the contents of the directory at ``path``."""
    path_str: str = args["path"]
    target = Path(path_str)

    if not target.exists():
        return ToolResult(
            ok=False,
            error=f"目录不存在: \"{path_str}\"",
        )

    if not target.is_dir():
        return ToolResult(
            ok=False,
            error=f"路径不是目录: \"{path_str}\"",
        )

    try:
        entries: list[str] = []
        for entry in sorted(target.iterdir()):
            suffix = "/" if entry.is_dir() else ""
            try:
                size = entry.stat().st_size if entry.is_file() else None
            except OSError:
                size = None
            line = entry.name + suffix
            if size is not None:
                line += f"  ({_format_size(size)})"
            entries.append(line)

        if not entries:
            return ToolResult(ok=True, content=f"目录 \"{path_str}\" 为空")

        result = "\n".join(entries)
        return ToolResult(ok=True, content=result)
    except OSError as exc:
        return ToolResult(ok=False, error=f"无法读取目录 \"{path_str}\": {exc}")


def _handle_read_file(args: dict[str, Any]) -> ToolResult:
    """Read the text content of the file at ``path``."""
    path_str: str = args["path"]
    target = Path(path_str)

    if not target.exists():
        return ToolResult(
            ok=False,
            error=f"文件不存在: \"{path_str}\"",
        )

    if not target.is_file():
        return ToolResult(
            ok=False,
            error=f"路径不是文件: \"{path_str}\"",
        )

    try:
        file_size = target.stat().st_size
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=f"无法获取文件 \"{path_str}\" 的信息: {exc}",
        )

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            if file_size > _MAX_FILE_BYTES:
                # Read a prefix of the file and mark truncation
                raw = fh.read(_MAX_FILE_BYTES)
                truncated = True
            else:
                raw = fh.read()
                truncated = False
    except OSError as exc:
        return ToolResult(
            ok=False,
            error=f"无法读取文件 \"{path_str}\": {exc}",
        )

    if truncated:
        content = (
            f"[文件过大，已截断] "
            f"原始大小约 {_format_size(file_size)}，"
            f"仅显示前 {_format_size(_MAX_FILE_BYTES)}：\n\n"
            + raw
        )
    else:
        content = raw

    return ToolResult(ok=True, content=content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_size(size_bytes: int) -> str:
    """Human-readable byte size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def create_current_time_tool() -> Tool:
    return Tool(
        name="current_time",
        description="获取当前的 UTC 日期和时间。不需要任何参数。",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handle_current_time,
        safety_level="read_only",
    )


def create_list_dir_tool() -> Tool:
    return Tool(
        name="list_dir",
        description=(
            "列出指定目录的内容，返回目录中的文件和子目录列表。"
            "需要提供 path 参数（字符串），可以是相对路径或绝对路径。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出内容的目录路径",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_handle_list_dir,
        safety_level="read_only",
    )


def create_read_file_tool() -> Tool:
    return Tool(
        name="read_file",
        description=(
            "读取指定文本文件的内容并返回。"
            "适合读取 README、源代码、配置文件等文本文件。"
            "需要提供 path 参数（字符串），可以是相对路径或绝对路径。"
            "文件不存在时会返回明确错误。大文件（超过 64 KB）会被截断。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_handle_read_file,
        safety_level="read_only",
    )


def register_all_readonly(registry) -> None:
    """Register all three read-only tools in *registry*."""
    registry.register(create_current_time_tool())
    registry.register(create_list_dir_tool())
    registry.register(create_read_file_tool())
