"""Read-only tools (Step 5).

All tools in this module observe the environment without side effects:

    current_time  - return the current local time in ISO-8601 format
    list_dir      - list files and subdirectories in a directory
    read_file     - read the contents of a text file

Every tool returns a ``ToolResult``; failures (missing file, invalid
path, oversized content, etc.) are reported as ``ok=False`` with a clear
error message - the agent loop never crashes on a tool failure.

v2: list_dir and read_file now resolve paths against the per-session
workspace when a ``WorkspaceManager`` and ``session_id_provider`` are
supplied.  This ensures the LLM sees the user's actual workspace
directory rather than the process CWD.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Callable

from claw.tools.base import Tool, ToolResult
from claw.utils import default_timezone_name
from claw.workspace.manager import WorkspaceManager, WorkspaceError

# Maximum file size before truncation: 64 KiB of UTF-8 text.
# Larger files are truncated with a clear marker in the returned content
# so the model knows the result is incomplete.
_MAX_FILE_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(
    path_str: str,
    workspace_manager: WorkspaceManager | None,
    session_id_provider: Callable[[], str] | None,
) -> Path:
    """Resolve *path_str* against the per-session workspace if available.

    When a workspace is bound to the session, relative paths are resolved
    inside the workspace root and boundary violations are rejected.  When
    no workspace is bound (backward-compat), the raw path is used as-is.

    When unlimited mode is enabled for the session, all workspace checks
    are bypassed and the path is resolved as-is.
    """
    if workspace_manager is not None and session_id_provider is not None:
        session_id = session_id_provider()
        if workspace_manager.is_unlimited(session_id):
            # Unlimited mode: resolve without workspace restrictions.
            return Path(path_str).resolve()
        ws = workspace_manager.get(session_id)
        if ws is not None:
            # Workspace is set - resolve relative paths inside it
            p = Path(path_str)
            if p.is_absolute():
                # Absolute paths are allowed but must be within workspace
                try:
                    p.resolve().relative_to(ws.resolve())
                    return p.resolve()
                except ValueError:
                    raise WorkspaceError(
                        f"path outside workspace: \"{path_str}\""
                    )
            # Relative path - resolve against workspace root
            resolved = (ws / p).resolve()
            try:
                resolved.relative_to(ws.resolve())
            except ValueError:
                raise WorkspaceError(
                    f"path outside workspace: \"{path_str}\""
                )
            return resolved
    # No workspace manager or no workspace set - use raw path (backward compat)
    return Path(path_str)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_current_time(args: dict[str, Any]) -> ToolResult:
    """Return the current time, optionally in a specific timezone."""
    tz_name = str(args.get("tz") or default_timezone_name()).strip() or default_timezone_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return ToolResult(
            ok=False,
            error=f"unknown timezone: \"{tz_name}\". Use IANA timezone like \"Asia/Shanghai\" or \"America/New_York\".",
        )
    now = datetime.now(tz).isoformat(timespec="seconds")
    return ToolResult(ok=True, content=f"{now} ({tz_name})")


def _make_list_dir_handler(
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        try:
            target = _resolve_path(path_str, workspace_manager, session_id_provider)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not target.exists():
            return ToolResult(
                ok=False,
                error=f"directory not found: \"{path_str}\"",
            )

        if not target.is_dir():
            return ToolResult(
                ok=False,
                error=f"path is not a directory: \"{path_str}\"",
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
                return ToolResult(ok=True, content=f"directory \"{path_str}\" is empty")

            result = "\n".join(entries)
            return ToolResult(ok=True, content=result)
        except OSError as exc:
            return ToolResult(ok=False, error=f"cannot read directory \"{path_str}\": {exc}")

    return handler


def _make_read_file_handler(
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        try:
            target = _resolve_path(path_str, workspace_manager, session_id_provider)
        except WorkspaceError as exc:
            return ToolResult(ok=False, error=str(exc))

        if not target.exists():
            return ToolResult(
                ok=False,
                error=f"file not found: \"{path_str}\"",
            )

        if not target.is_file():
            return ToolResult(
                ok=False,
                error=f"path is not a file: \"{path_str}\"",
            )

        try:
            file_size = target.stat().st_size
        except OSError as exc:
            return ToolResult(
                ok=False,
                error=f"cannot get file info \"{path_str}\": {exc}",
            )

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                if file_size > _MAX_FILE_BYTES:
                    raw = fh.read(_MAX_FILE_BYTES)
                    truncated = True
                else:
                    raw = fh.read()
                    truncated = False
        except OSError as exc:
            return ToolResult(
                ok=False,
                error=f"cannot read file \"{path_str}\": {exc}",
            )

        if truncated:
            content = (
                f"[file too large, truncated] "
                f"original size ~{_format_size(file_size)}, "
                f"showing first {_format_size(_MAX_FILE_BYTES)}:\n\n"
                + raw
            )
        else:
            content = raw

        return ToolResult(ok=True, content=content)

    return handler


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
        description=(
            f"Get the current date and time. Defaults to {default_timezone_name()} when tz is not set. "
            "Pass tz (e.g. \"Asia/Shanghai\") to get time in a specific timezone."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tz": {
                    "type": "string",
                    "description": f"IANA timezone name (optional), e.g. \"Asia/Shanghai\" or \"America/New_York\". Returns {default_timezone_name()} if omitted.",
                },
            },
            "required": [],
        },
        handler=_handle_current_time,
        safety_level="read_only",
        concurrency_safe=True,
    )


def create_list_dir_tool(
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
) -> Tool:
    return Tool(
        name="list_dir",
        description=(
            "List contents of a directory. Returns list of files and subdirectories. "
            "Requires path parameter (string), accepts relative or absolute paths. "
            "Relative paths are resolved against current workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list contents of",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_make_list_dir_handler(workspace_manager, session_id_provider),
        safety_level="read_only",
        concurrency_safe=True,
    )


def create_read_file_tool(
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
) -> Tool:
    return Tool(
        name="read_file",
        description=(
            "Read a text file and return its contents. "
            "Suitable for README, source code, config files. "
            "Requires path parameter (string), accepts relative or absolute paths. "
            "Relative paths are resolved against current workspace. "
            "Returns clear error if file does not exist. Files larger than 64 KB are truncated."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to read",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_make_read_file_handler(workspace_manager, session_id_provider),
        safety_level="read_only",
        concurrency_safe=True,
    )


def register_all_readonly(
    registry,
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
) -> None:
    """Register all three read-only tools in *registry*.

    When *workspace_manager* and *session_id_provider* are provided,
    ``list_dir`` and ``read_file`` resolve relative paths against the
    per-session workspace.
    """
    registry.register(create_current_time_tool())
    registry.register(create_list_dir_tool(workspace_manager, session_id_provider))
    registry.register(create_read_file_tool(workspace_manager, session_id_provider))
