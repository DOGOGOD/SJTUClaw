from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import claw.cli.repl as repl
import claw.gateway.server as gateway
import claw.workspace.manager as workspace_module
from claw.cli.commands import RuntimeState, parse_skill_invoke_result
from claw.session.store import SessionStore
from claw.workspace.manager import WorkspaceManager
from claw.workspace.rollback import WorkspaceRollbackManager


def test_parse_skill_invoke_result_preserves_pipes_in_task():
    parsed = parse_skill_invoke_result(
        "__SKILL_INVOKE__|course-report|比较 A | B 并生成报告"
    )
    assert parsed == ("course-report", "比较 A | B 并生成报告")
    assert parse_skill_invoke_result("ordinary command output") is None


@pytest.fixture()
def skill_gateway(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        workspace_module,
        "_BINDINGS_PATH",
        tmp_path / "runtime" / "bindings.json",
    )
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create_session(session_id="skill-session", title="Skill API")
    workspaces = WorkspaceManager()
    rollback = WorkspaceRollbackManager(
        workspaces,
        sessions,
        storage_root=tmp_path / "runtime" / "rollback",
    )
    monkeypatch.setattr(gateway, "_session_store", sessions)
    monkeypatch.setattr(gateway, "_workspace_manager", workspaces)
    monkeypatch.setattr(gateway, "_rollback_manager", rollback)
    monkeypatch.setattr(gateway, "_pet_state", MagicMock())
    monkeypatch.setattr(gateway, "_llm_ready", lambda: True)
    with gateway._active_turns_lock:
        gateway._active_turns.clear()
    return TestClient(gateway.app), sessions


def test_gateway_consumes_explicit_skill_marker_and_runs_agent(
    skill_gateway, monkeypatch
):
    client, sessions = skill_gateway
    captured = {}

    def fake_run_agent_turn(session_id, user_message, **kwargs):
        captured.update(
            session_id=session_id,
            user_message=user_message,
            skill_source=kwargs.get("skill_source"),
            skill_name=kwargs.get("skill_name"),
            skill_registry=kwargs.get("skill_registry"),
        )
        session = sessions.get(session_id)
        session.append_message("user", user_message)
        session.append_message("assistant", "显式调用验证完成")
        sessions.save(session)
        return "显式调用验证完成"

    monkeypatch.setattr(gateway, "run_agent_turn", fake_run_agent_turn)

    response = client.post(
        "/command",
        json={
            "sessionId": "skill-session",
            "command": "/skill course-report 生成显式调用验证报告",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "显式调用验证完成"
    assert not body["result"].startswith("__SKILL_INVOKE__")
    assert "reload_messages" in body["actions"]
    assert captured == {
        "session_id": "skill-session",
        "user_message": "生成显式调用验证报告",
        "skill_source": "explicit",
        "skill_name": "course-report",
        "skill_registry": gateway._skill_registry,
    }


def test_gateway_explicit_skill_reports_missing_llm_without_leaking_marker(
    skill_gateway, monkeypatch
):
    client, _ = skill_gateway
    monkeypatch.setattr(gateway, "_llm_ready", lambda: False)
    monkeypatch.setattr(gateway, "_llm_missing_reply", lambda: "请先配置模型")

    response = client.post(
        "/command",
        json={
            "sessionId": "skill-session",
            "command": "/skill course-report 生成报告",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "config_required"
    assert body["result"] == "请先配置模型"
    assert "__SKILL_INVOKE__" not in body["result"]


def test_cli_chat_turn_passes_explicit_skill_metadata(tmp_path: Path, monkeypatch):
    sessions = SessionStore(tmp_path / "sessions")
    session = sessions.create_session(session_id="cli-skill")
    captured = {}

    def fake_run_agent_turn(session_id, user_message, **kwargs):
        captured.update(
            session_id=session_id,
            user_message=user_message,
            skill_source=kwargs.get("skill_source"),
            skill_name=kwargs.get("skill_name"),
            skill_registry=kwargs.get("skill_registry"),
        )
        return "完成"

    monkeypatch.setattr(repl, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(repl, "_maybe_auto_title", lambda *_args: None)
    registry = object()
    state = RuntimeState(
        session_store=sessions,
        memory_store=MagicMock(),
        llm_client=MagicMock(),
        current_session_id=session.session_id,
        skill_registry=registry,
    )

    repl._handle_chat_turn(
        "生成课程报告",
        state,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        skill_source="explicit",
        skill_name="course-report",
    )

    assert captured == {
        "session_id": "cli-skill",
        "user_message": "生成课程报告",
        "skill_source": "explicit",
        "skill_name": "course-report",
        "skill_registry": registry,
    }
