"""Regression tests for UTF-8 and Windows-localized subprocess output."""

from __future__ import annotations

import json
from pathlib import Path

from claw.tools.base import ToolRegistry
from claw.tools.shell import create_new_shell_tool, create_run_command_tool
from claw.utils import decode_subprocess_output, force_utf8_stdio
from claw.workspace.manager import WorkspaceManager


def test_decode_subprocess_output_prefers_utf8() -> None:
    text = "工具调用失败：文件不存在"
    assert decode_subprocess_output(text.encode("utf-8")) == text


def test_decode_subprocess_output_supports_gbk_on_windows(monkeypatch) -> None:
    text = "系统找不到指定的文件。"
    monkeypatch.setattr("claw.utils.os.name", "nt")
    assert decode_subprocess_output(text.encode("gb18030")) == text


def test_force_utf8_stdio_reconfigures_available_streams(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class Stream:
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            calls.append((encoding, errors))

    monkeypatch.setattr("claw.utils.sys.stdin", Stream())
    monkeypatch.setattr("claw.utils.sys.stdout", Stream())
    monkeypatch.setattr("claw.utils.sys.stderr", Stream())

    force_utf8_stdio()

    assert calls == [("utf-8", "replace")] * 3


def test_shell_failure_keeps_chinese_output(monkeypatch, tmp_path: Path) -> None:
    """A failed tool call must not replace localized stderr with mojibake."""
    import claw.tools.shell as shell_module
    import claw.workspace.manager as workspace_module

    session_id = "encoding-regression"
    monkeypatch.setattr(
        workspace_module,
        "_BINDINGS_PATH",
        tmp_path / "workspace-state" / "bindings.json",
    )
    workspace = WorkspaceManager()
    workspace.set(session_id, str(tmp_path))
    registry = ToolRegistry()
    registry.register(create_new_shell_tool(workspace, lambda: session_id))
    registry.register(create_run_command_tool(workspace, lambda: session_id))
    assert registry.execute_by_name("new_shell", {}).ok

    class Completed:
        returncode = 1
        stdout = "工具调用失败：文件不存在\n".encode("gb18030")
        stderr = b""

    monkeypatch.setattr(shell_module.subprocess, "run", lambda *args, **kwargs: Completed())
    result = registry.execute_by_name("run_command", {"command": "missing-command"})

    assert not result.ok
    payload = json.loads(result.error or "{}")
    assert "工具调用失败：文件不存在" in payload["stdout"]
    assert "�" not in payload["stdout"]
