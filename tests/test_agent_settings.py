"""Contract tests for the WebUI Agent settings module."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from claw.gateway import server


def test_agent_settings_reports_each_runtime_installation(monkeypatch):
    monkeypatch.setattr(
        "claw.claude.resolve_claude_code_command",
        lambda: ("C:/tools/claude.exe",),
    )

    def missing_pi():
        raise RuntimeError("Pi command not found")

    monkeypatch.setattr("claw.pi.resolve_pi_command", missing_pi)
    monkeypatch.setattr(
        server,
        "setting_value",
        lambda name, default="": {
            "AGENT_BACKEND": "claude",
            "CLAUDE_CODE_PERMISSION_MODE": "default",
        }.get(name, default),
    )

    payload = server._agent_settings_payload()
    agents = {agent["id"]: agent for agent in payload["agents"]}

    assert payload["backend"] == "claude"
    assert agents["sjtuclaw"]["installed"] is True
    assert agents["claude"]["installed"] is True
    assert agents["claude"]["command"] == "C:/tools/claude.exe"
    assert agents["pi"]["installed"] is False
    assert "Pi command not found" in agents["pi"]["status"]


def test_agent_settings_rejects_unavailable_backend(monkeypatch):
    monkeypatch.setattr(
        server,
        "_agent_installations_payload",
        lambda: [
            {
                "id": "pi",
                "name": "Pi Agent",
                "installed": False,
            }
        ],
    )
    request = server.AgentSettingsRequest(backend="pi")

    with pytest.raises(HTTPException, match="尚未安装或不可用") as exc_info:
        server.update_agent_settings(request)

    assert exc_info.value.status_code == 400


def test_agent_settings_persists_backend_and_agent_options(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        server,
        "_agent_installations_payload",
        lambda: [
            {
                "id": "pi",
                "name": "Pi Agent",
                "installed": True,
            }
        ],
    )
    monkeypatch.setattr(server, "load_runtime_settings_raw", lambda: {"before": "value"})
    monkeypatch.setattr(
        server,
        "update_runtime_settings",
        lambda updates: captured.update(updates),
    )
    monkeypatch.setattr(server, "_apply_llm_runtime_config", lambda: None)
    monkeypatch.setattr(
        server,
        "_agent_settings_payload",
        lambda: {"backend": "pi", "agents": []},
    )

    response = server.update_agent_settings(
        server.AgentSettingsRequest(
            backend="pi",
            piProvider="anthropic",
            piModel="claude-sonnet",
            piThinking="high",
            piTrustTools=True,
        )
    )

    assert response["settings"]["backend"] == "pi"
    assert captured["AGENT_BACKEND"] == "pi"
    assert captured["PI_PROVIDER"] == "anthropic"
    assert captured["PI_MODEL"] == "claude-sonnet"
    assert captured["PI_THINKING"] == "high"
    assert captured["PI_TRUST_TOOLS"] == "true"


def test_llm_settings_payload_no_longer_contains_agent_backend(monkeypatch):
    monkeypatch.setattr(server, "setting_value", lambda _name, default="": default)

    payload = server._llm_settings_payload()

    assert "backend" not in payload
    assert "piProvider" not in payload
    assert "claudeModel" not in payload
