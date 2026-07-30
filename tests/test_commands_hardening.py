from __future__ import annotations

import asyncio
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from claw.cli.commands import (
    RuntimeState,
    handle_command,
    is_command,
    parse_skill_invoke_result,
)
from claw.memory.store import MemoryStore
from claw.session.store import (
    AUTO_MODE_METADATA_KEY,
    SANDBOX_MODE_METADATA_KEY,
    SessionStore,
    SessionStoreError,
)


def _state(tmp_path, **overrides):
    sessions = overrides.pop("session_store", SessionStore(tmp_path / "sessions"))
    if not sessions.list_summaries():
        sessions.create_session(session_id="session-a")
    values = {
        "session_store": sessions,
        "memory_store": MemoryStore(tmp_path / "memory"),
        "llm_client": MagicMock(),
        "current_session_id": "session-a",
    }
    values.update(overrides)
    return RuntimeState(**values)


def test_command_detection_normalizes_whitespace_and_intercepts_removed_subcommands():
    assert is_command("  /help  ") is True
    assert is_command("/memory\tstatus") is True
    assert is_command("/memory stats") is True
    assert is_command("/auto toggle") is True
    assert is_command("/unlimited toggle") is True
    assert is_command("/unknown") is False
    assert is_command("   ") is False


def test_empty_command_is_handled_without_exception(tmp_path):
    result = handle_command("   ", _state(tmp_path))
    assert "输入为空" in result


@pytest.mark.parametrize(
    ("command", "expected_commands"),
    [
        ("/session", ("/session new", "/session delete <sessionId>")),
        ("/memory", ("/memory add", "/memory status")),
        ("/workspace", ("/workspace set <路径>", "/workspace unset")),
        ("/sandbox", ("/sandbox on", "/sandbox off")),
        ("/skill", ("/skill list", "/skill <skill-name> <任务描述>")),
        ("/reflect", ("/reflect status", "/reflect now")),
        ("/cron", ("/cron list", "/cron delete <jobId>")),
        ("/rollback help", ("/rollback", "/rollback undo")),
    ],
)
def test_command_namespaces_show_descriptive_help(
    tmp_path, command, expected_commands
):
    result = handle_command(command, _state(tmp_path))

    assert result.startswith("用法:")
    for expected in expected_commands:
        assert expected in result
    assert len(result.splitlines()) >= 3


def test_namespace_help_is_formatted_for_webui_markdown(tmp_path):
    result = handle_command("/skill", _state(tmp_path), markdown=True)

    assert result.startswith("### 可用指令")
    assert "- `/skill list`：列出可用 Skills 及其简介" in result
    assert "- `/skill show <skill-name>`：查看指定 Skill 的详细说明" in result
    assert "**用法：** ``" not in result


def test_help_describes_sandbox_commands_and_workspace_behavior(tmp_path):
    state = _state(tmp_path)

    plain = handle_command("/help", state)
    markdown = handle_command("/help", state, markdown=True)

    for result in (plain, markdown):
        assert "/sandbox status" in result
        assert "/sandbox on" in result
        assert "/sandbox off" in result
        assert "/sandbox reset" not in result
        assert "私有" in result
        assert "/workspace" in result
        assert "明确绑定的目录" in result


def test_bare_sandbox_command_shows_subcommand_help(tmp_path):
    result = handle_command("/sandbox", _state(tmp_path))

    assert result.startswith("用法:")
    assert "/sandbox status" in result
    assert "/sandbox on" in result
    assert "/sandbox off" in result
    assert "/sandbox reset" not in result
    assert "Sandbox 模式:" not in result


@pytest.mark.parametrize("command", ["/sandbox help", "/sandbox ?"])
def test_sandbox_does_not_expose_help_aliases(tmp_path, command):
    result = handle_command(command, _state(tmp_path))

    assert result.startswith("未知 /sandbox 子命令")


def test_unknown_namespace_subcommand_includes_available_commands(tmp_path):
    result = handle_command("/memory unknown", _state(tmp_path))

    assert result.startswith("未知 /memory 子命令")
    assert "/memory add" in result
    assert "/memory status" in result


def test_empty_approval_list_explains_followup_commands(tmp_path):
    approvals = MagicMock()
    approvals.get_pending.return_value = []
    state = _state(tmp_path, approval_manager=approvals)

    result = handle_command("/approvals", state)

    assert "当前没有待审批" in result
    assert "/approve [approvalId]" in result
    assert "/reject [approvalId] [原因]" in result


def test_removed_mode_toggles_are_local_errors_and_do_not_change_state(tmp_path):
    workspace = MagicMock()
    workspace.is_unlimited.return_value = False
    state = _state(tmp_path, workspace_manager=workspace)

    auto_result = handle_command("/auto toggle", state)
    unlimited_result = handle_command("/unlimited toggle", state)

    assert "未知" in auto_result
    assert state.auto_mode is False
    assert "未知" in unlimited_result
    workspace.set_unlimited.assert_not_called()


def test_memory_add_parses_multiple_options_in_any_order(tmp_path):
    state = _state(tmp_path)

    result = handle_command(
        " /memory   add --tags Python,FastAPI "
        "--importance 5 --category project 构建 API 服务 ",
        state,
    )

    assert result.startswith("Added memory:")
    entry = state.memory_store.list()[0]
    assert entry.category == "project"
    assert entry.tags == ["fastapi", "python"]
    assert entry.importance == 5
    assert entry.content == "构建 API 服务"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/memory add --importance ten content", "importance 必须"),
        ("/memory add --unknown value content", "未知选项"),
        ("/memory add --tags content", "用法:"),
        ("/memory list --category", "用法:"),
    ],
)
def test_memory_command_rejects_malformed_options(tmp_path, command, expected):
    result = handle_command(command, _state(tmp_path))
    assert expected in result


def test_skill_command_accepts_normalized_spacing_and_preserves_pipe(tmp_path):
    skill = SimpleNamespace(
        name="demo",
        description="demo",
        instructions="demo",
        assets=[],
        references=[],
    )
    registry = MagicMock()
    registry.get_skill.return_value = skill
    state = _state(tmp_path, skill_registry=registry)

    result = handle_command(" /skill   demo   compare A | B ", state)

    assert parse_skill_invoke_result(result) == ("demo", "compare A | B")


def test_auto_mode_is_isolated_per_session(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create_session(session_id="session-a")
    sessions.create_session(session_id="session-b")
    state = _state(tmp_path, session_store=sessions)

    handle_command("/auto on", state)
    assert state.auto_mode is True

    handle_command("/session switch session-b", state)
    assert state.auto_mode is False

    handle_command("/auto on", state)
    handle_command("/session switch session-a", state)
    assert state.auto_mode is True


def test_auto_mode_survives_runtime_state_restart(tmp_path):
    sessions_dir = tmp_path / "sessions"
    first_store = SessionStore(sessions_dir)
    first_store.create_session(session_id="session-a")
    first_state = _state(tmp_path, session_store=first_store)

    assert "已开启" in handle_command("/auto on", first_state)
    assert first_store.get_metadata_flag(
        "session-a",
        AUTO_MODE_METADATA_KEY,
    ) is True

    restarted_store = SessionStore(sessions_dir)
    restarted_state = _state(tmp_path, session_store=restarted_store)

    assert restarted_state.auto_mode is True
    assert "当前已开启" in handle_command("/auto", restarted_state)


def test_fork_does_not_inherit_runtime_mode_preferences(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    source = store.create_session(session_id="source")
    store.set_metadata_flag("source", AUTO_MODE_METADATA_KEY, True)
    store.set_metadata_flag("source", SANDBOX_MODE_METADATA_KEY, True)
    source = store.get("source")
    source.append_message("user", "first")
    store.save(source)

    forked = store.fork_session_before_user_index("source", "forked", 1)

    assert forked is not None
    assert AUTO_MODE_METADATA_KEY not in forked.metadata
    assert SANDBOX_MODE_METADATA_KEY not in forked.metadata


def test_failed_session_delete_preserves_auxiliary_state(tmp_path):
    sessions = MagicMock()
    sessions.delete.side_effect = SessionStoreError("disk busy")
    rollback = MagicMock()
    rollback.session_guard.return_value = nullcontext()
    workspace = MagicMock()
    state = RuntimeState(
        session_store=sessions,
        memory_store=MagicMock(),
        llm_client=MagicMock(),
        current_session_id="session-a",
        workspace_manager=workspace,
        rollback_manager=rollback,
    )

    result = handle_command("/session delete session-a", state)

    assert "[错误]" in result
    rollback.disable.assert_not_called()
    workspace.unset.assert_not_called()
    workspace.set_unlimited.assert_not_called()


def test_workspace_unset_also_revokes_unlimited_mode(tmp_path):
    workspace = MagicMock()
    state = _state(tmp_path, workspace_manager=workspace)

    result = handle_command("/workspace unset", state)

    assert "UNLIMITED 模式已关闭" in result
    workspace.unset.assert_called_once_with("session-a")
    workspace.set_unlimited.assert_called_once_with("session-a", False)


def test_extra_arguments_cannot_trigger_destructive_commands(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create_session(session_id="session-a")
    workspace = MagicMock()
    rollback = MagicMock()
    state = _state(
        tmp_path,
        session_store=sessions,
        workspace_manager=workspace,
        rollback_manager=rollback,
    )

    assert "用法:" in handle_command(
        "/session delete session-a unexpected", state
    )
    assert sessions.exists("session-a")

    assert "用法:" in handle_command("/workspace unset unexpected", state)
    workspace.unset.assert_not_called()

    assert "用法:" in handle_command("/unlimited on unexpected", state)
    workspace.set_unlimited.assert_not_called()

    assert "用法:" in handle_command("/auto on unexpected", state)
    assert state.auto_mode is False

    assert "用法:" in handle_command("/rollback undo unexpected", state)
    rollback.undo.assert_not_called()


def test_reflection_time_rejects_impossible_clock_values(tmp_path):
    reflection = MagicMock()
    state = _state(tmp_path, reflection_manager=reflection)

    result = handle_command("/reflect time 99:99", state)

    assert "时间无效" in result
    reflection.update_config.assert_not_called()


def test_gateway_rejects_whitespace_only_command():
    from claw.gateway import server

    request = server.CommandRequest(command=" ")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.handle_command(request))
    assert exc_info.value.status_code == 400


def test_active_turn_guard_covers_mutating_commands():
    from claw.gateway.server import _mutating_command_session

    assert _mutating_command_session("/unlimited on", "s1") == "s1"
    assert _mutating_command_session("/compact", "s1") == "s1"
    assert _mutating_command_session("/rollback undo", "s1") == "s1"
    assert _mutating_command_session("/rollback on", "s1") == "s1"
    assert _mutating_command_session("/rollback off", "s1") == "s1"
    assert _mutating_command_session("/session delete s2", "s1") == "s2"
    assert _mutating_command_session("/workspace show", "s1") is None
    assert _mutating_command_session("/sandbox status", "s1") is None
    assert _mutating_command_session("/sandbox on", "s1") == "s1"
    assert _mutating_command_session("/sandbox off", "s1") == "s1"
    assert _mutating_command_session("/sandbox reset", "s1") is None
    assert _mutating_command_session("/pi", "s1") is None
    assert _mutating_command_session("/pi status", "s1") is None
    assert _mutating_command_session("/pi on", "s1") == "s1"
    assert _mutating_command_session("/claude", "s1") is None
    assert _mutating_command_session("/claude status", "s1") is None
    assert _mutating_command_session("/claude on", "s1") == "s1"


class _ShellWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def require(self, _session_id):
        return self.root

    def resolve(self, _session_id, sub_dir):
        return (self.root / sub_dir).resolve()

    def is_unlimited(self, _session_id):
        return True


def test_invalid_new_shell_does_not_destroy_existing_shell(tmp_path):
    from claw.tools import shell as shell_module

    workspace = _ShellWorkspace(tmp_path)
    session_id = "shell-preserve"
    create = shell_module._make_new_shell_handler(workspace, lambda: session_id)
    first = create({})
    assert first.ok is True
    with shell_module._shell_sessions_lock:
        original = shell_module._shell_sessions[session_id]

    invalid = create({"sub_dir": "missing"})

    assert invalid.ok is False
    with shell_module._shell_sessions_lock:
        assert shell_module._shell_sessions[session_id] is original
        shell_module._shell_sessions.pop(session_id, None)
    original.terminate()


def test_concurrent_commands_for_one_shell_are_serialized(tmp_path, monkeypatch):
    from claw.tools import shell as shell_module

    workspace = _ShellWorkspace(tmp_path)
    session_id = "shell-serialized"
    create = shell_module._make_new_shell_handler(workspace, lambda: session_id)
    run = shell_module._make_run_command_handler(workspace, lambda: session_id)
    assert create({}).ok is True

    guard = threading.Lock()
    active = 0
    max_active = 0

    def observed_run(_command, saved_cwd, _timeout):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.01)
            return SimpleNamespace(returncode=0), "", saved_cwd, False
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(shell_module, "_run_script", observed_run)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            future.result()
            for future in [
                pool.submit(run, {"command": "echo ok"})
                for _ in range(16)
            ]
        ]

    assert all(result.ok for result in results)
    assert max_active == 1
    with shell_module._shell_sessions_lock:
        shell = shell_module._shell_sessions.pop(session_id)
    shell.terminate()


def test_concurrent_create_file_has_exactly_one_winner(tmp_path):
    from claw.tools.update import _do_create

    target = tmp_path / "created.txt"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            future.result()
            for future in [
                pool.submit(_do_create, target, "created.txt")
                for _ in range(16)
            ]
        ]

    assert sum(result.ok for result in results) == 1
    assert target.read_text(encoding="utf-8") == ""


def test_overwrite_failure_preserves_original_file(tmp_path, monkeypatch):
    import claw.tools.update as update_module

    target = tmp_path / "stable.txt"
    target.write_text("original", encoding="utf-8")

    def fail_atomic_write(_path, _content):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(update_module, "atomic_write", fail_atomic_write)
    result = update_module._do_overwrite(target, "stable.txt", "replacement")

    assert not result.ok
    assert target.read_text(encoding="utf-8") == "original"
