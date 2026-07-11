"""Shell tools (Step 8): new_shell and run_command.

Cross-platform: ``cmd.exe`` on Windows, ``/bin/sh`` on POSIX.

``new_shell`` records the workspace root and initial cwd.
``run_command`` executes commands while tracking the shell's working
directory through a **state file** — after each command the real cwd
is captured from the subprocess output, so the tracked cwd always
reflects reality, not a guess.

State persistence across ``run_command`` calls:
    A temp file records the current cwd.  Every ``run_command``:
    1. Reads the saved cwd from the state file.
    2. Wraps the user command in a script that first ``cd`` to the saved
       cwd, runs the command, then outputs the final cwd and exit code.
    3. Parses the real cwd from the output and writes it back to the
       state file.
    4. Checks the real cwd against the workspace boundary.

On Windows, common Unix commands (rm, cp, mv, cat, ls, …) are
auto-translated to their cmd.exe equivalents so the LLM can use
familiar Unix syntax.  On POSIX no translation is needed.

Boundary enforcement:
    - Before every command the saved cwd is checked against workspace.
    - Known directory‑changing commands are pre-scanned — if the target
      would escape the workspace the command is **rejected before it
      runs**.
    - After every command the real cwd is checked — if it escaped the
      workspace the shell is terminated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from claw.tools.base import Tool, ToolResult
from claw.workspace.manager import WorkspaceManager, WorkspaceError

_IS_WINDOWS = os.name == "nt"
_DEFAULT_TIMEOUT = 60
_MAX_OUTPUT_BYTES = 64 * 1024

_CD_MARKER = "__CLAW_CD_MARKER__"
_EXIT_MARKER = "__CLAW_EXIT_MARKER__"


class ShellSession:
    """Tracks a shell's state for one session via a state file."""

    def __init__(self, workspace_root: Path, cwd: Path):
        self.workspace_root = workspace_root
        self.cwd: Path = cwd
        fd, self._state_path = tempfile.mkstemp(
            suffix=".txt", prefix="claw_shell_"
        )
        os.close(fd)
        self._write_state(str(cwd))

    def _write_state(self, cwd_str: str) -> None:
        Path(self._state_path).write_text(cwd_str, encoding="utf-8")

    def _read_state(self) -> str:
        try:
            return Path(self._state_path).read_text(encoding="utf-8").strip()
        except Exception:
            return str(self.cwd)

    def terminate(self) -> None:
        try:
            os.unlink(self._state_path)
        except OSError:
            pass


# session_id -> ShellSession
_shell_sessions: dict[str, ShellSession] = {}


# =========================================================================
# Platform abstraction
# =========================================================================


def _shell_exe() -> str:
    """Return the platform shell executable."""
    return "cmd.exe" if _IS_WINDOWS else "/bin/sh"


def _shell_args() -> list[str]:
    """Return shell arguments to execute a single command string."""
    return ["/C"] if _IS_WINDOWS else ["-c"]


def _cwd_command() -> str:
    """Return a command that prints the current working directory."""
    return "cd" if _IS_WINDOWS else "pwd"


def _build_script(command: str, cwd: str) -> str:
    """Build a platform-native wrapper script.

    The script:
        1. Changes to *cwd*.
        2. Runs the (possibly translated) *command*.
        3. Echoes the final cwd and exit code with markers.
    """
    translated = _translate_command(command) if _IS_WINDOWS else command

    if _IS_WINDOWS:
        return (
            f"@echo off\r\n"
            f"cd /d \"{cwd}\"\r\n"
            f"{translated}\r\n"
            f"echo {_CD_MARKER}%CD%{_CD_MARKER}\r\n"
            f"echo {_EXIT_MARKER}%ERRORLEVEL%{_EXIT_MARKER}\r\n"
        )
    else:
        # POSIX: /bin/sh script
        return (
            f"#!/bin/sh\n"
            f"cd \"{cwd}\" || exit 1\n"
            f"{translated}\n"
            f"echo \"{_CD_MARKER}$(pwd){_CD_MARKER}\"\n"
            f"echo \"{_EXIT_MARKER}$?{_EXIT_MARKER}\"\n"
        )


def _get_real_cwd(session: ShellSession, timeout: float = 5) -> str:
    """Run a cwd-printing command and return the real path."""
    try:
        proc = subprocess.run(
            [_shell_exe()] + _shell_args() + [_cwd_command()],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(session.cwd),
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
            ),
        )
        out = (proc.stdout or "").strip()
        if out:
            return out
    except Exception:
        pass
    return str(session.cwd)


def _check_in_workspace(cwd_str: str, workspace_root: Path) -> tuple[bool, str]:
    if not cwd_str:
        return False, "无法获取 shell 当前工作目录"
    try:
        cwd_path = Path(cwd_str).resolve()
    except Exception:
        return False, f"无法解析 shell 工作目录: {cwd_str}"
    ws_resolved = str(workspace_root.resolve())
    cwd_s = str(cwd_path)
    if cwd_s == ws_resolved or cwd_s.startswith(ws_resolved + os.sep):
        return True, ""
    return False, (
        f"shell 当前工作目录 \"{cwd_path}\" 不在 "
        f"workspace \"{workspace_root.resolve()}\" 内"
    )


# =========================================================================
# Unix → Windows command translation (Windows only)
# =========================================================================

_UNIX_TO_WIN: dict[str, str] = {
    "rm ": "del /f /q ",
    "cp ": "copy ",
    "mv ": "move ",
    "cat ": "type ",
    "ls ": "dir ",
    "pwd": "cd",
    "clear": "cls",
    "touch ": "type nul > ",
}


def _translate_command(command: str) -> str:
    """Translate common Unix commands to Windows cmd.exe equivalents."""
    if not _IS_WINDOWS:
        return command

    stripped = command.lstrip()
    leading_ws = command[: len(command) - len(stripped)]

    # Exact matches (no args)
    if stripped == "ls":
        return leading_ws + "dir"
    if stripped in ("pwd", "clear"):
        return leading_ws + _UNIX_TO_WIN.get(stripped, command)

    # Prefix matches (with args)
    for unix_prefix, win_replacement in sorted(
        _UNIX_TO_WIN.items(), key=lambda x: -len(x[0])
    ):
        if stripped.startswith(unix_prefix):
            return leading_ws + win_replacement + stripped[len(unix_prefix):]

    return command


# =========================================================================
# Pre-scan: directory-changing command escape detection
# =========================================================================

_CD_TARGET_RE = re.compile(r"^\s*(?:cd|chdir)\s+(.*)", re.IGNORECASE)
_CD_NO_ARG_RE = re.compile(r"^\s*(?:cd|chdir)\s*$", re.IGNORECASE)
_PUSHD_RE = re.compile(r"^\s*pushd\s+(.*)", re.IGNORECASE)
_SPLIT_RE = re.compile(r"(?:&&|\|\||[&|])\s*")


def _parse_cd_target(command: str) -> str | None:
    """Return the cd/chdir target, "" if cd alone, None if not a cd."""
    first = _SPLIT_RE.split(command, maxsplit=1)[0].strip()
    m = _CD_TARGET_RE.match(first)
    if m:
        target = m.group(1).strip()
        if _IS_WINDOWS and target.lower().startswith("/d "):
            target = target[3:].strip()
        return target
    if _CD_NO_ARG_RE.match(first):
        return ""  # cd alone — print cwd, don't change (both platforms)
    return None


def _parse_pushd_target(command: str) -> str | None:
    if not _IS_WINDOWS:
        return None  # pushd is Windows-only
    first = _SPLIT_RE.split(command, maxsplit=1)[0].strip()
    m = _PUSHD_RE.match(first)
    return m.group(1).strip() if m else None


def _resolve_target(target: str, current_cwd: Path) -> Path:
    p = Path(target)
    if p.is_absolute():
        return p.resolve()
    return (current_cwd / target).resolve()


def _precheck_directory_escape(
    command: str, cwd: Path, workspace_root: Path
) -> tuple[bool, Path | None, str]:
    """Check whether *command* would escape the workspace boundary."""
    ws_resolved = workspace_root.resolve()
    ws_str = str(ws_resolved)

    # cd / chdir
    target = _parse_cd_target(command)
    if target is not None:
        if target == "":
            return False, None, ""
        new_cwd = _resolve_target(target, cwd)
        cwd_s = str(new_cwd)
        if cwd_s != ws_str and not cwd_s.startswith(ws_str + os.sep):
            return True, new_cwd, (
                f"拒绝执行：cd 目标 \"{new_cwd}\" 超出 workspace \"{ws_resolved}\""
            )
        return False, new_cwd, ""

    # pushd (Windows only)
    target = _parse_pushd_target(command)
    if target is not None:
        new_cwd = _resolve_target(target, cwd)
        cwd_s = str(new_cwd)
        if cwd_s != ws_str and not cwd_s.startswith(ws_str + os.sep):
            return True, new_cwd, (
                f"拒绝执行：pushd 目标 \"{new_cwd}\" 超出 workspace \"{ws_resolved}\""
            )
        return False, new_cwd, ""

    return False, None, ""


# =========================================================================
# Pre-scan: file-path arguments escaping workspace
# =========================================================================

# Commands known to operate on file paths as primary arguments.
# We scan their non-flag tokens for path-like strings.
_PATH_AWARE_COMMANDS = {
    # Windows
    "del", "erase", "rmdir", "rd", "move", "ren", "rename",
    "copy", "xcopy", "robocopy", "type", "echo",
    # POSIX / cross-platform
    "rm", "mv", "cp", "cat", "touch", "chmod", "chown",
    "mkdir", "ln", "tee", "dd",
}

# Patterns that look like redirect / pipe (don't scan after these)
_REDIRECT_RE = re.compile(r"[<>|]")

# Regex to find path-like tokens: absolute paths, drive-letter paths,
# or relative paths containing separators.
_PATH_LIKE_RE = re.compile(
    r'(?:^|\s)(?:"([^"]+)"|'         # double-quoted
    r"(\S*[\\/]\S*)|"                 # unquoted with path separators
    r'([A-Za-z]:\\\S*))'              # Windows drive-letter path
)


def _precheck_command_paths(
    command: str,
    cwd: Path,
    workspace_root: Path,
) -> str:
    """Pre-scan *command* arguments for file paths that would escape the
    workspace.

    Only inspects arguments for commands known to operate on file paths
    (``_PATH_AWARE_COMMANDS``).  For each such command, resolves any
    path-like tokens against *cwd* and rejects the command if any
    resolved path lands outside *workspace_root*.

    Returns an empty string if the command is safe, or a human-readable
    rejection message otherwise.
    """
    # Quick check: does the command start with a path-aware command?
    stripped = command.lstrip()
    first_token = stripped.split(maxsplit=1)[0].lower().rstrip(":")
    if first_token not in _PATH_AWARE_COMMANDS and not any(
        stripped.lower().startswith(p) for p in _PATH_AWARE_COMMANDS
    ):
        return ""

    # Find all path-like tokens
    ws_str = str(workspace_root.resolve())
    for m in _PATH_LIKE_RE.finditer(stripped):
        candidate = m.group(1) or m.group(2) or m.group(3) or ""
        if not candidate.strip():
            continue
        # Skip flags / options
        if candidate.startswith("-") or candidate.startswith("/"):
            continue

        try:
            p = Path(candidate)
            if p.is_absolute() or str(p).startswith("\\"):
                resolved = p.resolve() if p.is_absolute() else (cwd / p).resolve()
            else:
                # Relative path with separators
                resolved = (cwd / candidate).resolve()
        except Exception:
            continue

        resolved_str = str(resolved)
        if resolved_str == ws_str or resolved_str.startswith(ws_str + os.sep):
            continue

        return (
            f"拒绝执行：命令中的路径 \"{candidate}\" 解析到 "
            f"\"{resolved}\"，超出 workspace \"{workspace_root.resolve()}\"。\n"
            f"所有文件操作必须在 workspace 内进行。"
        )

    return ""


# =========================================================================
# Script execution
# =========================================================================


def _run_script(
    command: str,
    cwd: str,
    timeout: int,
) -> tuple[object | None, str, str, bool]:
    """Build and run a platform wrapper script around *command*.

    Returns (proc, clean_stdout, real_cwd, timed_out).
    """
    script = _build_script(command, cwd)

    suffix = ".bat" if _IS_WINDOWS else ".sh"
    fd, tmp_script = tempfile.mkstemp(suffix=suffix, prefix="claw_cmd_")
    try:
        os.write(fd, script.encode("utf-8", errors="replace"))
        os.close(fd)

        if not _IS_WINDOWS:
            os.chmod(tmp_script, 0o755)

        proc = subprocess.run(
            [_shell_exe()] + _shell_args() + [tmp_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
            ),
        )
    except subprocess.TimeoutExpired:
        return None, "", cwd, True
    finally:
        try:
            os.unlink(tmp_script)
        except OSError:
            pass

    raw = (proc.stdout or "") + (proc.stderr or "")

    # Parse real cwd
    real_cwd = cwd
    m = re.search(
        re.escape(_CD_MARKER) + r"(.+?)" + re.escape(_CD_MARKER), raw
    )
    if m:
        real_cwd = m.group(1).strip()

    # Parse exit code
    exit_code = proc.returncode
    m2 = re.search(
        re.escape(_EXIT_MARKER) + r"(\d+)" + re.escape(_EXIT_MARKER), raw
    )
    if m2:
        try:
            exit_code = int(m2.group(1))
        except ValueError:
            pass

    # Strip marker lines from output
    clean = re.sub(
        re.escape(_CD_MARKER) + r".+?" + re.escape(_CD_MARKER) + r"\r?\n?",
        "", raw,
    )
    clean = re.sub(
        re.escape(_EXIT_MARKER) + r"\d+" + re.escape(_EXIT_MARKER) + r"\r?\n?",
        "", clean,
    )
    # Remove the cd line (first command in script)
    if _IS_WINDOWS:
        clean = re.sub(
            r'^[ \t]*cd /d ".+?"\r?\n?', "", clean, count=1, flags=re.MULTILINE,
        )
    else:
        clean = re.sub(
            r'^[ \t]*cd ".+?"\r?\n?', "", clean, count=1, flags=re.MULTILINE,
        )

    import types
    proc_fixed = types.SimpleNamespace(
        returncode=exit_code, stdout=clean, stderr="",
    )
    return proc_fixed, clean, real_cwd, False


# =========================================================================
# Tool description helper
# =========================================================================

def _shell_help() -> str:
    """Return platform-specific command help for the tool description."""
    if _IS_WINDOWS:
        return (
            "在 Windows cmd.exe 中执行一条命令。常用命令：\n"
            "- 删除文件: del /f /q <文件>\n"
            "- 复制文件: copy <源> <目标>\n"
            "- 移动/重命名: move <源> <目标>\n"
            "- 查看文件: type <文件>\n"
            "- 列出目录: dir\n"
            "- 显示当前目录: cd\n"
            "系统会自动翻译常见 Unix 命令（rm→del, cp→copy 等）。"
        )
    else:
        return (
            "在 /bin/sh 中执行一条命令。支持标准 Unix 命令："
            "rm, cp, mv, cat, ls, pwd, grep, find 等。"
        )


# =========================================================================
# Handlers
# =========================================================================


def _make_new_shell_handler(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        session_id = session_id_provider()
        ws = workspace_manager.require(session_id)

        old = _shell_sessions.pop(session_id, None)
        if old is not None:
            old.terminate()

        sub_dir: str = args.get("sub_dir", "")
        if sub_dir:
            try:
                cwd = workspace_manager.resolve(session_id, sub_dir)
            except WorkspaceError as exc:
                return ToolResult(ok=False, error=str(exc))
            if not cwd.is_dir():
                return ToolResult(
                    ok=False,
                    error=f"sub_dir 不是目录或不存在: \"{sub_dir}\"",
                )
        else:
            cwd = ws

        shell = ShellSession(ws, cwd)
        _shell_sessions[session_id] = shell

        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "tool": "new_shell",
                    "workspace": str(ws),
                    "cwd": str(cwd),
                    "shell": _shell_exe(),
                    "result": "shell 已启动",
                },
                ensure_ascii=False,
            ),
        )

    return handler


def _make_run_command_handler(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        command: str = args["command"]
        timeout: int = args.get("timeout", _DEFAULT_TIMEOUT)
        session_id = session_id_provider()

        shell = _shell_sessions.get(session_id)
        if shell is None:
            return ToolResult(
                ok=False,
                error=(
                    "当前没有已启动的 shell。"
                    "请先调用 new_shell 启动一个 shell，"
                    "然后再使用 run_command。"
                ),
            )

        # Read saved cwd from state file
        saved_cwd = shell._read_state()
        shell.cwd = Path(saved_cwd).resolve()

        # 1. Pre-check cwd still in workspace
        in_ws, reason = _check_in_workspace(saved_cwd, shell.workspace_root)
        if not in_ws:
            shell.terminate()
            _shell_sessions.pop(session_id, None)
            return ToolResult(
                ok=False,
                error=f"{reason}。shell 已被终止，请重新调用 new_shell。",
            )

        # 2. Pre-scan for directory escape (cd / pushd)
        escapes, predicted_cwd, reject_reason = _precheck_directory_escape(
            command, shell.cwd, shell.workspace_root
        )
        if escapes:
            return ToolResult(ok=False, error=reject_reason)

        # 2b. Pre-scan command arguments for paths that escape the workspace.
        #     This catches `del C:\\outside\\file`, `rm /etc/passwd`, etc.
        path_escape_reason = _precheck_command_paths(
            command, shell.cwd, shell.workspace_root
        )
        if path_escape_reason:
            return ToolResult(ok=False, error=path_escape_reason)

        # 3. Execute command via platform wrapper script
        proc, clean_stdout, real_cwd, timed_out = _run_script(
            command, saved_cwd, timeout
        )

        if timed_out or proc is None:
            result_obj = {
                "tool": "run_command",
                "ok": False,
                "command": command,
                "cwd": saved_cwd,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "timed_out": True,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "error": f"命令执行超时（{timeout} 秒）",
            }
            return ToolResult(
                ok=False,
                error=json.dumps(result_obj, ensure_ascii=False),
            )

        # 4. Update state with real cwd
        if real_cwd:
            shell._write_state(real_cwd)
            shell.cwd = Path(real_cwd).resolve()
        elif predicted_cwd is not None:
            shell._write_state(str(predicted_cwd))
            shell.cwd = predicted_cwd

        # 5. Post-check cwd in workspace
        in_ws, reason = _check_in_workspace(
            real_cwd or saved_cwd, shell.workspace_root
        )
        if not in_ws:
            shell.terminate()
            _shell_sessions.pop(session_id, None)
            err_obj = {
                "tool": "run_command",
                "ok": False,
                "command": command,
                "cwd": real_cwd or saved_cwd,
                "exit_code": proc.returncode,
                "stdout": clean_stdout,
                "stderr": "",
                "timed_out": False,
                "stdout_truncated": len(clean_stdout) > _MAX_OUTPUT_BYTES,
                "stderr_truncated": False,
                "error": f"{reason}。shell 已被终止，请重新调用 new_shell。",
            }
            return ToolResult(
                ok=False,
                error=json.dumps(err_obj, ensure_ascii=False),
            )

        # 6. Truncate + build result
        stdout_text = clean_stdout
        stdout_truncated = len(stdout_text) > _MAX_OUTPUT_BYTES
        if stdout_truncated:
            stdout_text = (
                stdout_text[:_MAX_OUTPUT_BYTES]
                + f"\n\n[输出已截断，原始长度 {len(clean_stdout)} 字节]"
            )

        exit_code = proc.returncode
        ok = exit_code == 0

        result_obj = {
            "tool": "run_command",
            "ok": ok,
            "command": command,
            "cwd": real_cwd or saved_cwd,
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": "",
            "timed_out": False,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": False,
        }
        if exit_code != 0:
            platform_hint = ""
            if _IS_WINDOWS:
                platform_hint = (
                    "如果使用了 Unix 命令（如 rm/cp/mv），系统已自动翻译为 "
                    "Windows 对应命令。检查文件路径是否正确。"
                )
            result_obj["error"] = f"命令退出码: {exit_code}。" + platform_hint

        if ok:
            return ToolResult(
                ok=True,
                content=json.dumps(result_obj, ensure_ascii=False),
            )
        else:
            return ToolResult(
                ok=False,
                error=json.dumps(result_obj, ensure_ascii=False),
            )

    return handler


# =========================================================================
# Tool definition factories
# =========================================================================


def create_new_shell_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Tool:
    return Tool(
        name="new_shell",
        description=(
            f"在 workspace 内启动一个新的 shell（{_shell_exe()}）。"
            "如果之前已有 shell，旧 shell 会先被终止。"
            "新 shell 的初始工作目录是 workspace 根目录。"
            "启动后可使用 run_command 执行命令，cd 效果会在后续命令中保留。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sub_dir": {
                    "type": "string",
                    "description": "workspace 内的子目录（可选，默认为 workspace 根目录）",
                }
            },
            "required": [],
        },
        handler=_make_new_shell_handler(workspace_manager, session_id_provider),
        safety_level="shell",
    )


def create_run_command_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
) -> Tool:
    return Tool(
        name="run_command",
        description=(
            f"{_shell_help()}\n"
            "多次 run_command 复用同一个 shell 状态：cd 效果会保留到后续命令。"
            "如果没有已启动的 shell，请先调用 new_shell。"
            "每次执行前后都会检查 shell 的真实工作目录，越界会终止 shell。"
            "可选参数 timeout（秒，默认 60）。"
            "返回包含：是否成功、命令、cwd、退出码、stdout、stderr、是否超时。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令",
                    "minLength": 1,
                },
                "timeout": {
                    "type": "integer",
                    "description": "命令超时时间（秒），默认 60",
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["command"],
        },
        handler=_make_run_command_handler(workspace_manager, session_id_provider),
        safety_level="shell",
    )
