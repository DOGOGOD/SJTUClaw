from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import claw.gateway.server as gateway
import claw.workspace.manager as workspace_module
from claw.session.store import SessionStore
from claw.workspace.manager import WorkspaceManager
from claw.workspace.rollback import WorkspaceRollbackManager


@pytest.fixture()
def rollback_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        workspace_module,
        "_BINDINGS_PATH",
        tmp_path / "runtime" / "bindings.json",
    )
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create_session(session_id="api-session", title="API")
    workspaces = WorkspaceManager()
    rollback = WorkspaceRollbackManager(
        workspaces,
        sessions,
        storage_root=tmp_path / "runtime" / "rollback",
    )
    monkeypatch.setattr(gateway, "_session_store", sessions)
    monkeypatch.setattr(gateway, "_workspace_manager", workspaces)
    monkeypatch.setattr(gateway, "_rollback_manager", rollback)
    with gateway._active_turns_lock:
        gateway._active_turns.clear()
    return TestClient(gateway.app), sessions, workspaces, rollback


def test_gateway_rollback_preview_and_apply(rollback_client, tmp_path):
    client, sessions, _, rollback = rollback_client
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "app.txt"
    target.write_text("before", encoding="utf-8")

    response = client.post(
        "/workspace", json={"sessionId": "api-session", "path": str(workspace)}
    )
    assert response.status_code == 200
    assert response.json()["rollback"]["enabled"] is True

    session = sessions.get("api-session")
    checkpoint_id = rollback.create_turn_checkpoint(
        "api-session", session, message_id="api-message", message_preview="API change"
    )
    session.append_message(
        "user", "API change", message_id="api-message",
        rollback_checkpoint_id=checkpoint_id,
    )
    session.append_message("assistant", "changed")
    sessions.save(session)
    target.write_text("after", encoding="utf-8")
    (workspace / "new.txt").write_text("new", encoding="utf-8")

    messages = client.get("/sessions/api-session/messages").json()
    assert messages["rollback"]["enabled"] is True
    assert messages["messages"][0]["rollbackCheckpointId"] == checkpoint_id

    preview = client.post(
        "/sessions/api-session/rollback/preview",
        json={"checkpointId": checkpoint_id},
    )
    assert preview.status_code == 200
    assert preview.json()["preview"]["filesToRestore"] == 1
    assert preview.json()["preview"]["filesToDelete"] == 1

    applied = client.post(
        "/sessions/api-session/rollback",
        json={"checkpointId": checkpoint_id},
    )
    assert applied.status_code == 200
    assert applied.json()["messages"] == []
    assert target.read_text(encoding="utf-8") == "before"
    assert not (workspace / "new.txt").exists()


def test_gateway_command_actions_cover_webui_navigation_and_compact(rollback_client):
    client, sessions, _, _ = rollback_client

    created = client.post(
        "/command",
        json={"sessionId": "api-session", "command": "/session new"},
    )
    assert created.status_code == 200
    assert "switch_session" in created.json()["actions"]
    assert created.json()["switchToSessionId"] != "api-session"

    switched = client.post(
        "/command",
        json={"sessionId": created.json()["switchToSessionId"], "command": "/session switch api-session"},
    )
    assert switched.status_code == 200
    assert switched.json()["switchToSessionId"] == "api-session"
    assert "switch_session" in switched.json()["actions"]

    compact = client.post(
        "/command",
        json={"sessionId": "api-session", "command": "/compact"},
    )
    assert compact.status_code == 200
    assert "无需压缩" in compact.json()["result"]
    assert "reload_messages" not in compact.json()["actions"]

    deleted = client.post(
        "/command",
        json={"sessionId": "api-session", "command": "/session delete api-session"},
    )
    assert deleted.status_code == 200
    assert "clear_session" in deleted.json()["actions"]
    assert not sessions.exists("api-session")


def test_gateway_rollback_requires_workspace(rollback_client):
    client, _, _, _ = rollback_client
    response = client.post("/sessions/api-session/rollback/preview", json={})
    assert response.status_code == 409
    assert "workspace" in response.json()["detail"]


def test_gateway_rejects_invalid_checkpoint_target(rollback_client, tmp_path):
    client, _, _, _ = rollback_client
    workspace = tmp_path / "invalid-target"
    workspace.mkdir()
    assert client.post(
        "/workspace", json={"sessionId": "api-session", "path": str(workspace)}
    ).status_code == 200
    response = client.post(
        "/sessions/api-session/rollback/preview",
        json={"checkpointId": "not-a-checkpoint"},
    )
    assert response.status_code == 409
    assert "正整数" in response.json()["detail"]


def test_gateway_rejects_rollback_during_active_turn(rollback_client, tmp_path):
    client, _, _, _ = rollback_client
    workspace = tmp_path / "busy-project"
    workspace.mkdir()
    assert client.post(
        "/workspace", json={"sessionId": "api-session", "path": str(workspace)}
    ).status_code == 200
    with gateway._active_turns_lock:
        gateway._active_turns["api-session"] = __import__("threading").Event()
    try:
        response = client.post("/sessions/api-session/rollback/preview", json={})
        assert response.status_code == 409
        assert "正在运行" in response.json()["detail"]
    finally:
        with gateway._active_turns_lock:
            gateway._active_turns.clear()


def test_gateway_rejects_workspace_mutation_during_active_turn(
    rollback_client, tmp_path
):
    client, _, _, _ = rollback_client
    workspace = tmp_path / "busy-mutation"
    workspace.mkdir()
    with gateway._active_turns_lock:
        gateway._active_turns["api-session"] = __import__("threading").Event()
    try:
        set_response = client.post(
            "/workspace", json={"sessionId": "api-session", "path": str(workspace)}
        )
        unset_response = client.delete(
            "/workspace", params={"sessionId": "api-session"}
        )
        command_response = client.post(
            "/command",
            json={"sessionId": "api-session", "command": f"/workspace set {workspace}"},
        )
        assert set_response.status_code == 409
        assert unset_response.status_code == 409
        assert "任务正在运行" in command_response.json()["result"]
    finally:
        with gateway._active_turns_lock:
            gateway._active_turns.clear()


def test_rebinding_hides_old_message_rollback_button(rollback_client, tmp_path):
    client, sessions, _, rollback = rollback_client
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    assert client.post(
        "/workspace",
        json={"sessionId": "api-session", "path": str(first_workspace)},
    ).status_code == 200
    session = sessions.get("api-session")
    checkpoint_id = rollback.create_turn_checkpoint(
        "api-session", session, message_id="old-message", message_preview="old"
    )
    session.append_message(
        "user", "old", message_id="old-message", rollback_checkpoint_id=checkpoint_id
    )
    sessions.save(session)

    assert client.post(
        "/workspace",
        json={"sessionId": "api-session", "path": str(second_workspace)},
    ).status_code == 200
    message = client.get("/sessions/api-session/messages").json()["messages"][0]
    assert "rollbackCheckpointId" not in message
    assert "rollbackAvailable" not in message
