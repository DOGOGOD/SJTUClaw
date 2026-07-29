from __future__ import annotations

from pathlib import Path

import claw.cli.main as cli_main


def _feed_inputs(monkeypatch, values: list[str]) -> None:
    answers = iter(values)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))


def test_setup_wizard_has_no_agent_backend_or_pi_configuration(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    env_path.write_text(
        "AGENT_BACKEND=pi\n"
        "CUSTOM_KEEP=unchanged\n"
        "LLM_MODEL=old-model\n",
        encoding="utf-8",
    )
    example_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_ENV_PATH", env_path)
    monkeypatch.setattr(cli_main, "_ENV_EXAMPLE", example_path)

    decisions = iter([True, True, True, True])
    monkeypatch.setattr(
        cli_main,
        "_prompt_yn",
        lambda _prompt, default=True: next(decisions),
    )
    monkeypatch.setattr(
        cli_main,
        "_setup_llm",
        lambda: {
            "LLM_API_KEY": "secret-key",
            "LLM_BASE_URL": "https://models.example/v1",
            "LLM_MODEL": "new-model",
        },
    )
    monkeypatch.setattr(
        cli_main,
        "_setup_preferences",
        lambda: {"WEB_TOOL_ENABLED": "true", "CLAW_TIMEZONE": "Asia/Shanghai"},
    )
    monkeypatch.setattr(
        cli_main,
        "_setup_gateway",
        lambda: {
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": "8000",
            "GATEWAY_OPEN_BROWSER": "false",
        },
    )
    monkeypatch.setattr(
        cli_main,
        "_setup_advanced",
        lambda: {
            "LLM_CONTEXT_WINDOW": "64000",
            "LLM_MAX_OUTPUT_TOKENS": "4096",
            "LLM_MAX_RETRIES": "2",
            "LLM_REQUEST_TIMEOUT": "120",
        },
    )
    monkeypatch.setattr(
        cli_main,
        "_setup_channels",
        lambda: {
            "QQ_ENABLED": "true",
            "QQ_APP_ID": "app-id",
            "QQ_CLIENT_SECRET": "qq-secret",
            "QQ_ALLOW_FROM": "openid",
            "QQ_MSG_FORMAT": "markdown",
            "QQ_ACK_MESSAGE": "",
        },
    )

    assert cli_main._cmd_setup() == 0

    saved = env_path.read_text(encoding="utf-8")
    assert "AGENT_BACKEND=pi" in saved
    assert "CUSTOM_KEEP=unchanged" in saved
    assert "LLM_MODEL=new-model" in saved
    assert "WEB_TOOL_ENABLED=true" in saved
    assert "GATEWAY_HOST=127.0.0.1" in saved
    assert "QQ_MSG_FORMAT=markdown" in saved
    assert saved.count("AGENT_BACKEND=") == 1

    output = capsys.readouterr().out
    assert "secret-key" not in output
    assert "qq-secret" not in output
    assert "欢迎使用 SJTUClaw" in output
    assert "Pi" not in output
    assert "Agent 后端" not in output
    assert ".env 中其他配置保持不变" not in output


def test_setup_gateway_local_mode_preserves_existing_remote_security(
    monkeypatch,
):
    monkeypatch.setattr(
        cli_main,
        "_read_env",
        lambda: {
            "GATEWAY_HOST": "0.0.0.0",
            "GATEWAY_PORT": "9000",
            "GATEWAY_OPEN_BROWSER": "true",
            "GATEWAY_API_TOKEN": "existing-secure-token-that-stays",
            "GATEWAY_ALLOWED_ORIGINS": "http://192.168.1.8:9000",
        },
    )
    _feed_inputs(monkeypatch, ["1", "", ""])

    updates = cli_main._setup_gateway()

    assert updates == {
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_PORT": "9000",
        "GATEWAY_OPEN_BROWSER": "true",
    }


def test_setup_gateway_lan_mode_generates_token_and_requires_origin(
    monkeypatch,
):
    monkeypatch.setattr(cli_main, "_read_env", lambda: {})
    monkeypatch.setattr(cli_main.secrets, "token_urlsafe", lambda _size: "generated-token")
    monkeypatch.setattr(
        cli_main,
        "_suggest_lan_origin",
        lambda port: f"http://192.168.1.20:{port}",
    )
    _feed_inputs(monkeypatch, ["2", "8123", "", ""])

    updates = cli_main._setup_gateway()

    assert updates == {
        "GATEWAY_HOST": "0.0.0.0",
        "GATEWAY_PORT": "8123",
        "GATEWAY_OPEN_BROWSER": "false",
        "GATEWAY_API_TOKEN": "generated-token",
        "GATEWAY_ALLOWED_ORIGINS": "http://192.168.1.20:8123",
    }


def test_setup_advanced_uses_current_defaults_and_optional_tavily(monkeypatch):
    monkeypatch.setattr(
        cli_main,
        "_read_env",
        lambda: {
            "LLM_CONTEXT_WINDOW": "64000",
            "LLM_MAX_OUTPUT_TOKENS": "8192",
            "LLM_MAX_RETRIES": "3",
            "LLM_REQUEST_TIMEOUT": "90",
        },
    )
    _feed_inputs(monkeypatch, ["", "", "", "", ""])

    updates = cli_main._setup_advanced()

    assert updates == {
        "LLM_CONTEXT_WINDOW": "64000",
        "LLM_MAX_OUTPUT_TOKENS": "8192",
        "LLM_MAX_RETRIES": "3",
        "LLM_REQUEST_TIMEOUT": "90",
    }


def test_setup_qq_manual_mode_includes_channel_preferences(monkeypatch):
    monkeypatch.setattr(cli_main, "_read_env", lambda: {})
    monkeypatch.setattr(cli_main.getpass, "getpass", lambda _prompt: "qq-secret")
    _feed_inputs(
        monkeypatch,
        ["", "2", "app-id", "user-openid", "text", "正在处理"],
    )

    updates = cli_main._setup_qq()

    assert updates == {
        "QQ_ENABLED": "true",
        "QQ_APP_ID": "app-id",
        "QQ_CLIENT_SECRET": "qq-secret",
        "QQ_ALLOW_FROM": "user-openid",
        "QQ_MSG_FORMAT": "text",
        "QQ_ACK_MESSAGE": "正在处理",
    }


def test_prompt_origins_rejects_paths_and_normalizes_trailing_slashes(
    monkeypatch,
    capsys,
):
    _feed_inputs(
        monkeypatch,
        [
            "http://192.168.1.10:8000/settings",
            "http://192.168.1.10:8000/, https://claw.example/",
        ],
    )

    origins = cli_main._prompt_origins("", 8000)

    assert origins == "http://192.168.1.10:8000,https://claw.example"
    assert "至少需要一个完整的" in capsys.readouterr().out
