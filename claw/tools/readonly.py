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

import heapq
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Any, Callable

from claw.tools.base import Tool, ToolResult
from claw.utils import default_timezone_name
from claw.paths import main_dir
from claw.workspace.manager import WorkspaceManager, WorkspaceError

if TYPE_CHECKING:
    from claw.sandbox import SandboxManager

# Maximum file size before truncation: 64 KiB of UTF-8 text.
# Larger files are truncated with a clear marker in the returned content
# so the model knows the result is incomplete.
_MAX_FILE_BYTES = 64 * 1024
_MAX_DIR_ENTRIES = 1000


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
    inside the workspace root and boundary violations are rejected.  An
    unbound managed session is sandboxed to the stable application root.

    When unlimited mode is enabled for the session, all workspace checks
    are bypassed and the path is resolved as-is.
    """
    if workspace_manager is not None and session_id_provider is not None:
        session_id = session_id_provider()
        if workspace_manager.is_unlimited(session_id):
            # Unlimited mode: resolve without workspace restrictions.
            return Path(path_str).resolve()
        # An unbound session is still sandboxed to the stable main directory.
        # Falling through to an arbitrary absolute path here let an
        # auto-executed read_file call expose any local file.
        root = (workspace_manager.get(session_id) or main_dir()).resolve()
        p = Path(path_str)
        resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise WorkspaceError(
                f"path outside workspace: \"{path_str}\""
            )
        return resolved
    # No explicit workspace: use the runtime's stable main directory rather
    # than inheriting whichever cwd happened to launch the Gateway/WebUI.
    path = Path(path_str)
    if path.is_absolute():
        return path.resolve()
    return (main_dir() / path).resolve()


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
    sandbox_manager: SandboxManager | None = None,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        if (
            sandbox_manager is not None
            and workspace_manager is not None
            and session_id_provider is not None
        ):
            session_id = session_id_provider()
            try:
                use_sandbox = sandbox_manager.should_use(
                    session_id, workspace_manager
                )
                if use_sandbox:
                    entries = sandbox_manager.list_dir(
                        session_id, workspace_manager, path_str
                    )
                    lines = []
                    for entry in entries[:_MAX_DIR_ENTRIES]:
                        suffix = "/" if entry.kind in {"directory", "dir"} else ""
                        line = entry.name + suffix
                        if entry.kind in {"file", "regular"}:
                            line += f"  ({_format_size(entry.size)})"
                        lines.append(line)
                    if len(entries) > _MAX_DIR_ENTRIES:
                        lines.append(
                            f"...[目录条目已截断，仅显示前 {_MAX_DIR_ENTRIES} 项]"
                        )
                    return ToolResult(
                        ok=True,
                        content=(
                            "\n".join(lines)
                            if lines
                            else f'directory "{path_str}" is empty'
                        ),
                    )
            except Exception as exc:
                return ToolResult(ok=False, error=str(exc))

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
            visible = heapq.nsmallest(
                _MAX_DIR_ENTRIES + 1,
                target.iterdir(),
                key=lambda entry: entry.name.casefold(),
            )
            truncated = len(visible) > _MAX_DIR_ENTRIES
            for entry in visible[:_MAX_DIR_ENTRIES]:
                suffix = "/" if entry.is_dir() else ""
                try:
                    size = entry.stat().st_size if entry.is_file() else None
                except OSError:
                    size = None
                line = entry.name + suffix
                if size is not None:
                    line += f"  ({_format_size(size)})"
                entries.append(line)
            if truncated:
                entries.append(
                    f"...[目录条目已截断，仅显示前 {_MAX_DIR_ENTRIES} 项]"
                )

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
    sandbox_manager: SandboxManager | None = None,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        if (
            sandbox_manager is not None
            and workspace_manager is not None
            and session_id_provider is not None
        ):
            session_id = session_id_provider()
            try:
                use_sandbox = sandbox_manager.should_use(
                    session_id, workspace_manager
                )
                if use_sandbox:
                    payload, truncated = sandbox_manager.read_file(
                        session_id,
                        workspace_manager,
                        path_str,
                        max_bytes=_MAX_FILE_BYTES,
                    )
                    raw = payload.decode("utf-8", errors="replace")
                    content = (
                        f"[file too large, truncated] "
                        f"showing first {_format_size(_MAX_FILE_BYTES)}:\n\n{raw}"
                        if truncated
                        else raw
                    )
                    return ToolResult(ok=True, content=content)
            except Exception as exc:
                return ToolResult(ok=False, error=str(exc))

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
            with open(target, "rb") as fh:
                payload = fh.read(_MAX_FILE_BYTES + 1)
        except OSError as exc:
            return ToolResult(
                ok=False,
                error=f"cannot read file \"{path_str}\": {exc}",
            )
        truncated = len(payload) > _MAX_FILE_BYTES
        raw = payload[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")

        if truncated:
            content = (
                f"[file too large, truncated] "
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
    sandbox_manager: SandboxManager | None = None,
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
        handler=_make_list_dir_handler(
            workspace_manager, session_id_provider, sandbox_manager
        ),
        safety_level="read_only",
        concurrency_safe=True,
    )


def create_read_file_tool(
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
    sandbox_manager: SandboxManager | None = None,
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
        handler=_make_read_file_handler(
            workspace_manager, session_id_provider, sandbox_manager
        ),
        safety_level="read_only",
        concurrency_safe=True,
    )


def register_all_readonly(
    registry,
    workspace_manager: WorkspaceManager | None = None,
    session_id_provider: Callable[[], str] | None = None,
    sandbox_manager: SandboxManager | None = None,
) -> None:
    """Register all three read-only tools in *registry*.

    When *workspace_manager* and *session_id_provider* are provided,
    ``list_dir`` and ``read_file`` resolve relative paths against the
    per-session workspace.
    """
    registry.register(create_current_time_tool())
    registry.register(
        create_list_dir_tool(
            workspace_manager, session_id_provider, sandbox_manager
        )
    )
    registry.register(
        create_read_file_tool(
            workspace_manager, session_id_provider, sandbox_manager
        )
    )
