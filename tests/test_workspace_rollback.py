from __future__ import annotations

from pathlib import Path
import os
import subprocess
import threading

import pytest

import claw.workspace.manager as workspace_module
import claw.workspace.rollback as rollback_module
from claw.agent.loop import run_agent_turn
from claw.cli.commands import RuntimeState, handle_command
from claw.config import CompactionConfig
from claw.context.compaction_worker import CompactionWorker
from claw.llm.protocol import AgentResponse, ToolCallRequest
from claw.session.store import SessionStore
from claw.tools.base import Tool, ToolRegistry, ToolResult
from claw.workspace.manager import WorkspaceManager
from claw.workspace.rollback import RollbackError, WorkspaceRollbackManager


class _Memory:
    pass


class _LLM:
    pass


class _Context:
    def build_messages(self, session, **kwargs):
        return [message.to_dict() for message in session.messages]

    def get_tool_definitions(self, registry):
        return registry.list_definitions()


class _SequenceLLM:
    def __init__(self, *responses):
        self.responses = list(responses)

    def chat_with_tools(self, messages, tool_defs):
        return self.responses.pop(0)


@pytest.fixture()
def rollback_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        workspace_module,
        "_BINDINGS_PATH",
        tmp_path / "runtime" / "bindings.json",
    )
    sessions = SessionStore(tmp_path / "sessions")
    session = sessions.create_session(session_id="s1", title="Original")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = WorkspaceManager()
    rollback = WorkspaceRollbackManager(
        manager,
        sessions,
        storage_root=tmp_path / "runtime" / "rollback",
    )
    return sessions, session, workspace, manager, rollback


def test_no_workspace_disables_rollback(rollback_env):
    _, session, _, _, rollback = rollback_env
    assert rollback.status(session.session_id)["enabled"] is False
    with pytest.raises(RollbackError, match="未设置 workspace"):
        rollback.preview(session.session_id)


def test_invalid_rollback_target_is_a_user_error(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    with pytest.raises(RollbackError, match="正整数"):
        rollback.preview("s1", "not-a-checkpoint")


def test_restore_files_and_conversation_together(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    original = workspace / "original.txt"
    removed_later = workspace / "remove-me.txt"
    original.write_text("before", encoding="utf-8")
    removed_later.write_text("keep", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)

    message_id = "msg_turn_1"
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id=message_id, message_preview="change files"
    )
    session.append_message(
        "user", "change files", message_id=message_id,
        rollback_checkpoint_id=checkpoint_id,
    )
    session.append_message("assistant", "done")
    session.title = "Changed title"
    sessions.save(session)

    original.write_text("after", encoding="utf-8")
    removed_later.unlink()
    (workspace / "created.txt").write_text("new", encoding="utf-8")
    (workspace / "empty").mkdir()

    preview = rollback.preview("s1", checkpoint_id)
    assert preview.messages_to_remove == 2
    assert "original.txt" in preview.restore_files
    assert "created.txt" in preview.delete_paths

    result = rollback.rollback("s1", checkpoint_id)
    assert result["restored"] >= 2
    assert original.read_text(encoding="utf-8") == "before"
    assert removed_later.read_text(encoding="utf-8") == "keep"
    assert not (workspace / "created.txt").exists()
    assert not (workspace / "empty").exists()
    assert session.messages == []
    assert session.title == "Original"


def test_rollback_restores_pre_compaction_session_snapshot(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    session.append_message("user", "old question")
    session.append_message("assistant", "old answer")
    sessions.save(session)
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="future", message_preview="future turn"
    )

    session.append_message("user", "future", message_id="future", rollback_checkpoint_id=checkpoint_id)
    session.append_message("assistant", "future answer")
    # Simulate a compaction after the checkpoint without deleting raw history.
    session.summary = "summary including future context"
    session.last_consolidated = 3
    session.touch()
    sessions.save(session)

    rollback.rollback("s1", checkpoint_id)
    assert [message.content for message in session.messages] == ["old question", "old answer"]
    assert session.summary == ""
    assert session.last_consolidated == 0


def test_checkpoint_session_snapshot_preserves_internal_message_fields(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    session.append_message(
        "assistant",
        "internal event",
        injected_event="subagent_result",
        subagent_task_id="task-7",
        latency_ms=123,
    )
    original = session.messages[0]
    sessions.save(session)
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="future", message_preview="future turn"
    )
    session.messages[0].content = "mutated"
    session.append_message(
        "user", "future", message_id="future", rollback_checkpoint_id=checkpoint_id
    )
    sessions.save(session)

    rollback.rollback("s1", checkpoint_id)
    restored = session.messages[0]
    assert restored.message_id == original.message_id
    assert restored.injected_event == "subagent_result"
    assert restored.subagent_task_id == "task-7"
    assert restored.latency_ms == 123


def test_undo_restores_state_before_rollback(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    target = workspace / "value.txt"
    target.write_text("one", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="write two"
    )
    session.append_message("user", "write two", message_id="m1", rollback_checkpoint_id=checkpoint_id)
    session.append_message("assistant", "done")
    sessions.save(session)
    target.write_text("two", encoding="utf-8")

    rollback.rollback("s1", checkpoint_id)
    assert target.read_text(encoding="utf-8") == "one"
    assert session.messages == []

    rollback.undo("s1")
    assert target.read_text(encoding="utf-8") == "two"
    assert [message.content for message in session.messages] == ["write two", "done"]


def test_failed_session_persist_compensates_workspace_and_live_session(
    rollback_env, monkeypatch
):
    sessions, session, workspace, manager, rollback = rollback_env
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="change"
    )
    session.append_message(
        "user", "change", message_id="m1", rollback_checkpoint_id=checkpoint_id
    )
    session.append_message("assistant", "done")
    sessions.save(session)
    target.write_text("after", encoding="utf-8")

    original_save = sessions.save
    attempts = 0

    def fail_once(value, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated persist failure")
        return original_save(value, *args, **kwargs)

    monkeypatch.setattr(sessions, "save", fail_once)
    with pytest.raises(RollbackError, match="已恢复回退前状态"):
        rollback.rollback("s1", checkpoint_id)

    assert target.read_text(encoding="utf-8") == "after"
    assert [message.content for message in session.messages] == ["change", "done"]
    with rollback._connect() as conn:
        status = conn.execute(
            "SELECT status FROM operations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    assert status == "COMPENSATED"


def test_startup_retries_interrupted_compensation(rollback_env, monkeypatch):
    sessions, session, workspace, manager, rollback = rollback_env
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="change"
    )
    session.append_message(
        "user", "change", message_id="m1", rollback_checkpoint_id=checkpoint_id
    )
    session.append_message("assistant", "done")
    sessions.save(session)
    target.write_text("after", encoding="utf-8")

    original_save = sessions.save
    monkeypatch.setattr(
        sessions,
        "save",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("storage offline")),
    )
    with pytest.raises(RollbackError, match="重启后将自动重试"):
        rollback.rollback("s1", checkpoint_id)
    with rollback._connect() as conn:
        assert conn.execute(
            "SELECT status FROM operations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0] == "COMPENSATING"

    monkeypatch.setattr(sessions, "save", original_save)
    recovered = WorkspaceRollbackManager(
        manager, sessions, storage_root=rollback.storage_root
    )
    assert recovered.recover_incomplete_operations() == 0
    assert target.read_text(encoding="utf-8") == "after"
    assert [message.content for message in session.messages] == ["change", "done"]
    with recovered._connect() as conn:
        assert conn.execute(
            "SELECT status FROM operations ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0] == "ROLLED_BACK"


def test_workspace_rebinding_invalidates_old_checkpoints(rollback_env, tmp_path):
    _, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="old root"
    )
    other = tmp_path / "other-workspace"
    other.mkdir()
    manager.set("s1", str(other))
    rollback.enable("s1", session)
    with pytest.raises(RollbackError, match="没有找到"):
        rollback.preview("s1", checkpoint_id)


def test_latest_checkpoint_is_stable_when_timestamps_match(rollback_env, monkeypatch):
    sessions, session, workspace, manager, rollback = rollback_env
    monkeypatch.setattr(rollback_module, "now_iso", lambda: "2026-01-01T00:00:00+00:00")
    target = workspace / "value.txt"
    target.write_text("one", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)

    first = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="first"
    )
    session.append_message("user", "first", message_id="m1", rollback_checkpoint_id=first)
    target.write_text("two", encoding="utf-8")
    second = rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="second"
    )
    session.append_message("user", "second", message_id="m2", rollback_checkpoint_id=second)
    sessions.save(session)
    target.write_text("three", encoding="utf-8")

    result = rollback.rollback("s1")
    assert result["checkpointId"] == second
    assert target.read_text(encoding="utf-8") == "two"
    assert [message.content for message in session.messages] == ["first"]


def test_restore_replaces_directory_symlink_before_writing_children(
    rollback_env, tmp_path
):
    sessions, session, workspace, manager, rollback = rollback_env
    directory = workspace / "nested"
    directory.mkdir()
    (directory / "value.txt").write_text("snapshot", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "value.txt"
    outside_file.write_text("outside-safe", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="symlink test"
    )
    session.append_message(
        "user", "symlink test", message_id="m1", rollback_checkpoint_id=checkpoint_id
    )
    sessions.save(session)
    (directory / "value.txt").unlink()
    directory.rmdir()
    try:
        os.symlink(outside, directory, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"当前环境不允许创建测试 symlink: {exc}")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(directory), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"当前 Windows 环境不允许创建测试 junction: {junction.stderr}")

    rollback.rollback("s1", checkpoint_id)
    assert not directory.is_symlink()
    assert (directory / "value.txt").read_text(encoding="utf-8") == "snapshot"
    assert outside_file.read_text(encoding="utf-8") == "outside-safe"


def test_preview_does_not_store_cancelled_workspace_versions(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="preview"
    )
    before = {path for path in rollback.objects_dir.rglob("*") if path.is_file()}
    (workspace / "cancelled.txt").write_text("never checkpointed", encoding="utf-8")
    rollback.preview("s1", checkpoint_id)
    after = {path for path in rollback.objects_dir.rglob("*") if path.is_file()}
    assert after == before


def test_disable_purges_checkpoint_metadata_and_unreferenced_objects(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    (workspace / "tracked.txt").write_text("content", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    assert any(path.is_file() for path in rollback.objects_dir.rglob("*"))
    with rollback._connect() as conn:
        encoded = conn.execute(
            "SELECT session_json FROM checkpoints LIMIT 1"
        ).fetchone()[0]
    assert encoded.startswith("zlib:")

    rollback.disable("s1")
    with rollback._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id='s1'"
        ).fetchone()[0] == 0
    assert not any(path.is_file() for path in rollback.objects_dir.rglob("*"))


def test_unlimited_warning_covers_all_removed_turns(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    first = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="normal", partial=False
    )
    session.append_message("user", "normal", message_id="m1", rollback_checkpoint_id=first)
    second = rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="unlimited", partial=True
    )
    session.append_message(
        "user", "unlimited", message_id="m2", rollback_checkpoint_id=second
    )
    sessions.save(session)
    assert rollback.preview("s1", first).partial is True
    assert rollback.rollback("s1", first)["partial"] is True


def test_new_turn_invalidates_single_step_undo(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    first = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="first"
    )
    session.append_message("user", "first", message_id="m1", rollback_checkpoint_id=first)
    sessions.save(session)
    rollback.rollback("s1", first)
    assert rollback.status("s1")["undoAvailable"] is True
    rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="new branch"
    )
    assert rollback.status("s1")["undoAvailable"] is False
    with pytest.raises(RollbackError, match="没有找到"):
        rollback.undo("s1")


def test_cli_rollback_command(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="cli turn"
    )
    session.append_message("user", "cli turn", message_id="m1", rollback_checkpoint_id=checkpoint_id)
    sessions.save(session)
    state = RuntimeState(
        session_store=sessions,
        memory_store=_Memory(),
        llm_client=_LLM(),
        current_session_id="s1",
        workspace_manager=manager,
        rollback_manager=rollback,
    )
    assert "回退完成" in handle_command("/rollback", state)
    assert session.messages == []
    assert "[错误]" in handle_command("/rollback abc", state)


def test_help_lists_complete_rollback_usage(rollback_env):
    sessions, _, _, manager, rollback = rollback_env
    state = RuntimeState(
        session_store=sessions,
        memory_store=_Memory(),
        llm_client=_LLM(),
        current_session_id="s1",
        workspace_manager=manager,
        rollback_manager=rollback,
    )
    plain = handle_command("/help", state)
    markdown = handle_command("/help", state, markdown=True)
    for usage in (
        "/rollback",
        "/rollback <n>",
        "/rollback <checkpointId>",
        "/rollback list",
        "/rollback status",
        "/rollback undo",
    ):
        assert usage in plain
        assert usage in markdown
    assert "未设置 workspace 时不支持回退" in markdown


def test_cli_workspace_set_restores_previous_binding_when_snapshot_fails(
    rollback_env, tmp_path, monkeypatch
):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    other = tmp_path / "cannot-snapshot"
    other.mkdir()
    state = RuntimeState(
        session_store=sessions,
        memory_store=_Memory(),
        llm_client=_LLM(),
        current_session_id="s1",
        workspace_manager=manager,
        rollback_manager=rollback,
    )
    monkeypatch.setattr(
        rollback, "enable", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RollbackError("snapshot failed")
        )
    )
    result = handle_command(f"/workspace set {other}", state)
    assert "[错误]" in result
    assert manager.get("s1") == workspace.resolve()


def test_agent_turn_automatically_creates_checkpoint_and_rewinds_shell_like_change(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    changed = workspace / "generated.txt"
    registry = ToolRegistry()

    def write_file(_args):
        changed.write_text("created by tool", encoding="utf-8")
        return ToolResult(ok=True, content="written")

    registry.register(Tool(
        name="write_probe",
        description="write a test file",
        input_schema={"type": "object", "properties": {}},
        handler=write_file,
        # The checkpoint layer is intentionally independent from tool type;
        # this simulates an opaque shell/process side effect.
        safety_level="read_only",
    ))
    llm = _SequenceLLM(
        AgentResponse(tool_calls=[ToolCallRequest(name="write_probe", args={}, call_id="call-1")]),
        AgentResponse(final="done"),
    )

    run_agent_turn(
        "s1",
        "create it",
        session_store=sessions,
        context_builder=_Context(),
        tool_registry=registry,
        llm_client=llm,
        rollback_manager=rollback,
        auto_mode=True,
    )

    assert changed.read_text(encoding="utf-8") == "created by tool"
    user = next(message for message in session.messages if message.role == "user")
    assert user.rollback_checkpoint_id
    rollback.rollback("s1", user.rollback_checkpoint_id)
    assert not changed.exists()
    assert session.messages == []


def test_background_compaction_discards_result_after_session_revision_changes(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    session = sessions.create_session(session_id="compact-race")
    for index in range(8):
        session.append_message("user" if index % 2 == 0 else "assistant", f"message {index} " * 20)
    sessions.save(session)

    started = threading.Event()
    release = threading.Event()

    class BlockingLLM:
        def chat(self, messages):
            started.set()
            assert release.wait(timeout=5)
            return "valid summary"

    worker = CompactionWorker(
        BlockingLLM(),
        sessions,
        config=CompactionConfig(keep_recent_tokens=20, keep_recent_messages_min=2),
    )
    assert worker.submit(session)
    assert started.wait(timeout=5)
    # A rollback uses the same monotonic revision invalidation.
    session.touch()
    changed_revision = session.revision
    release.set()
    assert worker.wait(timeout=5)
    assert session.revision == changed_revision
    assert session.summary == ""
    assert session.last_consolidated == 0
