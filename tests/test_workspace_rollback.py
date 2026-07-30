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
from claw.session.store import (
    AUTO_MODE_METADATA_KEY,
    SANDBOX_MODE_METADATA_KEY,
    SessionStore,
)
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


def test_setting_workspace_does_not_enable_rollback(rollback_env):
    _, session, workspace, manager, rollback = rollback_env

    manager.set("s1", str(workspace))

    status = rollback.status("s1")
    assert status["enabled"] is False
    assert status["workspace"] == str(workspace.resolve())
    assert status["preference"] is None
    assert rollback.create_turn_checkpoint(
        "s1",
        session,
        message_id="not-enabled",
        message_preview="must not capture",
    ) is None
    with pytest.raises(RollbackError, match="/rollback on"):
        rollback.preview("s1")


def test_legacy_implicit_preference_requires_explicit_reenable(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    old_binding = rollback.status("s1")["bindingId"]
    old_checkpoint = rollback.create_turn_checkpoint(
        "s1",
        session,
        message_id="legacy",
        message_preview="legacy implicit checkpoint",
    )
    with rollback._connect() as conn:
        conn.execute(
            "UPDATE preferences SET explicit=0 WHERE session_id='s1'"
        )

    restarted = WorkspaceRollbackManager(
        manager,
        sessions,
        storage_root=rollback.storage_root,
    )

    assert restarted.preference("s1") is None
    assert restarted.status("s1")["enabled"] is False
    assert restarted.list_checkpoints("s1") == []
    assert restarted.create_turn_checkpoint(
        "s1",
        session,
        message_id="still-disabled",
        message_preview="must not capture",
    ) is None

    restarted.enable("s1", session)
    assert restarted.status("s1")["enabled"] is True
    assert restarted.status("s1")["bindingId"] != old_binding
    assert all(
        item["checkpointId"] != old_checkpoint
        for item in restarted.list_checkpoints("s1")
    )


def test_rollback_preserves_current_runtime_mode_preferences(rollback_env):
    sessions, session, _, _, rollback = rollback_env
    snapshot = session.to_snapshot_dict()
    snapshot.setdefault("metadata", {})[AUTO_MODE_METADATA_KEY] = False
    snapshot["metadata"][SANDBOX_MODE_METADATA_KEY] = False
    session.metadata[AUTO_MODE_METADATA_KEY] = True
    session.metadata[SANDBOX_MODE_METADATA_KEY] = True

    rollback._restore_session(session, snapshot)
    sessions.save(session)

    assert session.metadata[AUTO_MODE_METADATA_KEY] is True
    assert session.metadata[SANDBOX_MODE_METADATA_KEY] is True


def test_invalid_rollback_target_is_a_user_error(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    with pytest.raises(RollbackError, match="正整数"):
        rollback.preview("s1", "not-a-checkpoint")


def test_checkpoint_scan_ignores_dependency_and_cache_directories(rollback_env):
    _, _, workspace, _, rollback = rollback_env
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    for directory in ("node_modules", ".venv", "__pycache__", ".pytest_cache"):
        target = workspace / directory
        target.mkdir()
        (target / "large-generated.bin").write_bytes(b"x" * 1024)

    manifest = rollback._scan_workspace(workspace, store_blobs=False)

    assert "src/app.py" in manifest
    assert not any(
        path.split("/", 1)[0]
        in {"node_modules", ".venv", "__pycache__", ".pytest_cache"}
        for path in manifest
    )


def test_unchanged_files_reuse_incremental_hash_cache(rollback_env, monkeypatch):
    _, session, workspace, manager, rollback = rollback_env
    large = workspace / "large.bin"
    large.write_bytes(b"x" * (2 * 1024 * 1024))
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="first capture"
    )

    def unexpected_capture(*_args, **_kwargs):
        raise AssertionError("unchanged cached files must not be read again")

    monkeypatch.setattr(rollback, "_store_blob", unexpected_capture)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="cached capture"
    )

    assert checkpoint_id
    with rollback._connect() as conn:
        payload = conn.execute(
            "SELECT manifest_json FROM checkpoints WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()[0]
    scan = rollback._decode_manifest(payload)
    assert scan.stats["filesReused"] == 1
    assert scan.stats["bytesRead"] == 0


def test_truncated_cached_object_is_recaptured(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    source = workspace / "cached.bin"
    source.write_bytes(b"healthy content")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    first = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="first"
    )
    with rollback._connect() as conn:
        first_payload = conn.execute(
            "SELECT manifest_json FROM checkpoints WHERE checkpoint_id=?",
            (first,),
        ).fetchone()[0]
    digest = rollback._decode_manifest(first_payload).entries["cached.bin"]["hash"]
    rollback._object_path(digest).write_bytes(b"bad")

    second = rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="repair cache"
    )

    assert second
    assert rollback._object_path(digest).read_bytes() == b"healthy content"


def test_snapshot_budget_prefers_small_files(rollback_env):
    _, _, workspace, _, rollback = rollback_env
    (workspace / "large.bin").write_bytes(b"x" * 12)
    (workspace / "small-a.bin").write_bytes(b"a" * 4)
    (workspace / "small-b.bin").write_bytes(b"b" * 4)
    rollback.max_snapshot_bytes = 8

    scan = rollback._scan_workspace_report(workspace, store_blobs=False)

    assert {"small-a.bin", "small-b.bin"} <= set(scan.entries)
    assert "large.bin" in scan.ignored_paths


def test_parallel_capture_uses_bounded_workers(rollback_env, monkeypatch):
    _, _, workspace, _, rollback = rollback_env
    (workspace / "one.bin").write_bytes(b"1")
    (workspace / "two.bin").write_bytes(b"2")
    rollback.scan_workers = 2
    original_store = rollback._store_blob
    barrier = threading.Barrier(2, timeout=5)
    worker_names: set[str] = set()
    names_lock = threading.Lock()

    def synchronized_store(*args, **kwargs):
        with names_lock:
            worker_names.add(threading.current_thread().name)
        barrier.wait()
        return original_store(*args, **kwargs)

    monkeypatch.setattr(rollback, "_store_blob", synchronized_store)

    scan = rollback._scan_workspace_report(workspace, store_blobs=True)

    assert scan.complete is True
    assert len(worker_names) == 2
    assert all(name.startswith("rollback-scan") for name in worker_names)


def test_blob_capture_reads_source_only_once(rollback_env, monkeypatch):
    _, _, workspace, _, rollback = rollback_env
    source = workspace / "single-pass.bin"
    source.write_bytes(b"single pass")

    monkeypatch.setattr(
        rollback,
        "_hash_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("_store_blob must not hash the source a second time")
        ),
    )

    digest = rollback._store_blob(source, expected=source.stat())

    assert rollback._object_path(digest).read_bytes() == b"single pass"


def test_verified_blob_copy_rejects_corrupt_object(rollback_env):
    _, _, workspace, _, rollback = rollback_env
    source = workspace / "corrupt.object"
    destination = workspace / "restored.tmp"
    source.write_bytes(b"corrupt")

    with pytest.raises(RollbackError, match="校验失败"):
        rollback._copy_blob_verified(source, destination, "0" * 64)

    assert not destination.exists()


def test_snapshot_budget_progressively_captures_uncached_files(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    (workspace / "a.bin").write_bytes(b"a" * 8)
    (workspace / "b.bin").write_bytes(b"b" * 8)
    rollback.max_snapshot_bytes = 8
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)

    first = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="budget one"
    )
    second = rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="budget two"
    )

    with rollback._connect() as conn:
        first_row = conn.execute(
            "SELECT manifest_json,partial FROM checkpoints WHERE checkpoint_id=?",
            (first,),
        ).fetchone()
        second_row = conn.execute(
            "SELECT manifest_json,partial FROM checkpoints WHERE checkpoint_id=?",
            (second,),
        ).fetchone()
    assert first_row["partial"] == 1
    assert len(rollback._decode_manifest(first_row["manifest_json"]).entries) == 1
    assert second_row["partial"] == 0
    assert len(rollback._decode_manifest(second_row["manifest_json"]).entries) == 2


def test_oversized_file_is_left_untouched_by_partial_rollback(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    large = workspace / "large.bin"
    large.write_bytes(b"before" * 16)
    rollback.max_file_bytes = 16
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="partial"
    )

    large.write_bytes(b"after" * 16)
    created = workspace / "created.txt"
    created.write_text("remove me", encoding="utf-8")
    preview = rollback.preview("s1", checkpoint_id)

    assert preview.partial is True
    assert "large.bin" not in preview.restore_files
    assert "large.bin" not in preview.delete_paths
    assert "created.txt" in preview.delete_paths
    result = rollback.rollback("s1", checkpoint_id)
    assert result["partial"] is True
    assert large.read_bytes() == b"after" * 16
    assert not created.exists()


def test_ignored_file_path_protects_descendants_after_becoming_directory(
    rollback_env,
):
    _, session, workspace, manager, rollback = rollback_env
    ignored = workspace / "large.bin"
    ignored.write_bytes(b"x" * 64)
    rollback.max_file_bytes = 16
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="ignored path"
    )

    ignored.unlink()
    ignored.mkdir()
    descendant = ignored / "must-survive.txt"
    descendant.write_text("safe", encoding="utf-8")

    preview = rollback.preview("s1", checkpoint_id)
    assert "large.bin/must-survive.txt" not in preview.delete_paths

    rollback.rollback("s1", checkpoint_id)
    assert descendant.read_text(encoding="utf-8") == "safe"


def test_partial_safety_checkpoint_does_not_overwrite_uncaptured_file(
    rollback_env,
):
    _, session, workspace, manager, rollback = rollback_env
    target = workspace / "value.bin"
    target.write_bytes(b"before")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="small target"
    )

    target.write_bytes(b"after" * 16)
    rollback.max_file_bytes = 16
    result = rollback.rollback("s1", checkpoint_id)

    assert result["partial"] is True
    assert any("安全撤销" in warning for warning in result["warnings"])
    assert target.read_bytes() == b"after" * 16


def test_partial_safety_checkpoint_restores_captured_paths_and_supports_undo(
    rollback_env,
):
    _, session, workspace, manager, rollback = rollback_env
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="before"
    )

    target.write_text("after", encoding="utf-8")
    oversized = workspace / "large.bin"
    oversized.write_bytes(b"x" * 64)
    rollback.max_file_bytes = 16

    result = rollback.rollback("s1", checkpoint_id)
    assert result["partial"] is True
    assert target.read_text(encoding="utf-8") == "before"
    assert oversized.read_bytes() == b"x" * 64

    rollback.undo("s1")
    assert target.read_text(encoding="utf-8") == "after"
    assert oversized.read_bytes() == b"x" * 64


def test_rollback_reuses_safety_scan_instead_of_rescanning_workspace(
    rollback_env, monkeypatch
):
    _, session, workspace, manager, rollback = rollback_env
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    checkpoint_id = rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="before"
    )
    target.write_text("after", encoding="utf-8")

    original_scan = rollback._scan_workspace_report
    scan_count = 0

    def counted_scan(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(rollback, "_scan_workspace_report", counted_scan)

    rollback.rollback("s1", checkpoint_id)

    assert scan_count == 1
    assert target.read_text(encoding="utf-8") == "before"


def test_garbage_collect_removes_interrupted_capture_temp_file(rollback_env):
    _, _, _, _, rollback = rollback_env
    rollback.objects_dir.mkdir(parents=True, exist_ok=True)
    orphan = rollback.objects_dir / ".capture.interrupted.tmp"
    orphan.write_bytes(b"partial")
    stale_time = (
        rollback_module.time.time()
        - rollback_module._ORPHAN_CAPTURE_MAX_AGE_S
        - 1
    )
    os.utime(orphan, (stale_time, stale_time))

    removed = rollback.garbage_collect()

    assert removed == 1
    assert not orphan.exists()


def test_garbage_collect_preserves_recent_capture_temp_file(rollback_env):
    _, _, _, _, rollback = rollback_env
    active = rollback.objects_dir / ".capture.active.tmp"
    active.write_bytes(b"in progress")

    removed = rollback.garbage_collect()

    assert removed == 0
    assert active.read_bytes() == b"in progress"


def test_file_limit_stops_scan_without_claiming_full_coverage(rollback_env):
    _, _, workspace, _, rollback = rollback_env
    (workspace / "one.txt").write_text("1", encoding="utf-8")
    (workspace / "two.txt").write_text("2", encoding="utf-8")
    rollback.max_files = 1

    scan = rollback._scan_workspace_report(workspace, store_blobs=False)

    assert scan.complete is False
    assert scan.partial is True
    assert any("文件数量" in warning for warning in scan.warnings)


def test_incomplete_manifest_never_drives_path_deletion(rollback_env):
    _, _, workspace, _, rollback = rollback_env
    extra = workspace / "must-survive.txt"
    extra.write_text("safe", encoding="utf-8")
    wanted = rollback._decode_manifest({
        "__sjtuclawManifestVersion": 2,
        "entries": {},
        "ignoredPaths": [],
        "complete": False,
        "warnings": ["truncated"],
        "stats": {},
    })

    restored, deleted = rollback._apply_manifest(workspace, wanted)

    assert (restored, deleted) == (0, 0)
    assert extra.read_text(encoding="utf-8") == "safe"


def test_unknown_manifest_version_is_rejected(rollback_env):
    _, _, _, _, rollback = rollback_env

    with pytest.raises(RollbackError, match="不支持"):
        rollback._decode_manifest({
            "__sjtuclawManifestVersion": 999,
            "entries": {},
        })


def test_enabling_rollback_does_not_snapshot_workspace(rollback_env, monkeypatch):
    _, session, workspace, manager, rollback = rollback_env
    (workspace / "large-project-file.bin").write_bytes(b"x" * 1024)
    manager.set("s1", str(workspace))

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("workspace binding must not capture a baseline snapshot")

    monkeypatch.setattr(rollback, "_scan_workspace", unexpected_scan)

    status = rollback.enable("s1", session)

    assert status["enabled"] is True
    assert status["checkpointCount"] == 0
    assert rollback.list_checkpoints("s1") == []


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


def test_disable_persists_preference_and_stops_new_checkpoints(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    (workspace / "tracked.txt").write_text("content", encoding="utf-8")
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    rollback.create_turn_checkpoint(
        "s1", session, message_id="m1", message_preview="track content"
    )
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
    assert rollback.preference("s1") is False
    assert rollback.status("s1")["preference"] is False
    assert rollback.create_turn_checkpoint(
        "s1", session, message_id="m2", message_preview="must stay disabled"
    ) is None

    restarted = WorkspaceRollbackManager(
        manager,
        sessions,
        storage_root=rollback.storage_root,
    )
    assert restarted.preference("s1") is False
    assert restarted.create_turn_checkpoint(
        "s1", session, message_id="m3", message_preview="still disabled"
    ) is None

    restarted.enable("s1", session)
    assert restarted.preference("s1") is True
    assert restarted.create_turn_checkpoint(
        "s1", session, message_id="m4", message_preview="enabled again"
    )


def test_purge_removes_rollback_preference(rollback_env):
    _, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    rollback.disable("s1")

    rollback.purge("s1")

    assert rollback.preference("s1") is None


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


def test_cli_rollback_on_off_switch(rollback_env):
    sessions, session, workspace, manager, rollback = rollback_env
    manager.set("s1", str(workspace))
    rollback.enable("s1", session)
    state = RuntimeState(
        session_store=sessions,
        memory_store=_Memory(),
        llm_client=_LLM(),
        current_session_id="s1",
        workspace_manager=manager,
        rollback_manager=rollback,
    )

    assert "已关闭" in handle_command("/rollback off", state)
    assert "已关闭" in handle_command("/rollback status", state)
    assert rollback.create_turn_checkpoint(
        "s1", session, message_id="off", message_preview="off"
    ) is None
    assert "已关闭" in handle_command("/rollback list", state)

    rebound = workspace.parent / "rebound-workspace"
    rebound.mkdir()
    assert "已设置为" in handle_command(f"/workspace set {rebound}", state)
    assert manager.get("s1") == rebound.resolve()
    assert "已关闭" in handle_command("/rollback status", state)

    assert "已开启" in handle_command("/rollback on", state)
    assert "已启用" in handle_command("/rollback status", state)
    assert rollback.create_turn_checkpoint(
        "s1", session, message_id="on", message_preview="on"
    )


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
        "/rollback on",
        "/rollback off",
        "/rollback undo",
    ):
        assert usage in plain
        assert usage in markdown
    assert "未设置 workspace 时不支持回退" in markdown
    assert "设置 workspace 不会自动开启回退" in markdown
    assert "设置后不会自动开启回退" in handle_command("/workspace help", state)
    assert "设置 workspace 不会自动开启回退" in handle_command(
        "/rollback help", state
    )
    assert (
        "rollback功能仍不完善，workspace中文件过多时不建议使用"
        in handle_command("/rollback help", state)
    )


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
