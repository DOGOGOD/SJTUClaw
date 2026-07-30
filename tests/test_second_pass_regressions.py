from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


def test_rollback_connections_are_closed_after_context(tmp_path, monkeypatch):
    import claw.workspace.manager as workspace_module
    from claw.session.store import SessionStore
    from claw.workspace.manager import WorkspaceManager
    from claw.workspace.rollback import WorkspaceRollbackManager

    monkeypatch.setattr(
        workspace_module,
        "_BINDINGS_PATH",
        tmp_path / "bindings.json",
    )
    rollback = WorkspaceRollbackManager(
        WorkspaceManager(),
        SessionStore(tmp_path / "sessions"),
        storage_root=tmp_path / "rollback",
    )

    with rollback._connect() as connection:
        connection.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_skill_lifecycle_persists_seed_and_ages_never_used_skill(tmp_path):
    from claw.skills.usage import STATE_ARCHIVED, SkillUsageStore

    store = SkillUsageStore(tmp_path / "skills")
    assert store.apply_automatic_transitions(["demo-skill"]) == {}

    seeded = store.load_usage()
    assert "demo-skill" in seeded
    seeded["demo-skill"]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(days=100)
    ).isoformat()
    store.save_usage(seeded)

    changes = store.apply_automatic_transitions(
        ["demo-skill"],
        stale_after_days=30,
        archive_after_days=90,
    )

    assert changes == {"demo-skill": STATE_ARCHIVED}
    assert store.load_usage()["demo-skill"]["state"] == STATE_ARCHIVED


def test_context_governance_repairs_non_list_calls_and_idless_tool_results():
    from claw.context.governance import ContextGovernor, GovernanceConfig

    messages = [
        {
            "role": "assistant",
            "content": "保留可见回复",
            "tool_calls": 42,
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": "没有可关联的调用 ID",
        },
        {"role": "user", "content": "继续"},
    ]

    prepared = ContextGovernor().prepare_for_model(
        GovernanceConfig(max_tool_result_chars=8_000),
        messages,
    )

    assert prepared == [
        {"role": "assistant", "content": "保留可见回复"},
        {"role": "user", "content": "继续"},
    ]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_tool_schema_rejects_non_finite_numbers(value):
    from claw.tools.base import _validate_args

    errors = _validate_args(
        {"amount": value},
        {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    )

    assert errors
    assert "类型错误" in errors[0]


def test_runtime_settings_use_process_locks(tmp_path, monkeypatch):
    from claw import runtime_settings

    monkeypatch.setattr(runtime_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(
        runtime_settings,
        "SETTINGS_PATH",
        tmp_path / "runtime_settings.json",
    )
    monkeypatch.setattr(
        runtime_settings,
        "KEY_PATH",
        tmp_path / "runtime_settings.key",
    )
    acquired: list[str] = []

    class RecordingLock:
        def __init__(self, path, timeout):
            acquired.append(str(path))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(runtime_settings, "FileLock", RecordingLock)

    runtime_settings.update_runtime_settings(
        {"LLM_MODEL": "model", "LLM_API_KEY": "secret"}
    )

    assert str(runtime_settings.SETTINGS_PATH) + ".lock" in acquired
    assert str(runtime_settings.KEY_PATH) + ".lock" in acquired
    assert json.loads(runtime_settings.SETTINGS_PATH.read_text(encoding="utf-8"))
