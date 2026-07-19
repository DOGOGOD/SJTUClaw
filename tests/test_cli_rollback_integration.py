from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from claw.session.store import SessionStore


def test_real_cli_workspace_rollback_flow(tmp_path: Path):
    """Exercise the installed CLI entry path against an OpenAI-compatible stub."""
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    generated = workspace / "generated.txt"
    observations: dict[str, object] = {"agent_requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if body.get("tools"):
                observations["agent_requests"] = int(observations["agent_requests"]) + 1
                if observations["agent_requests"] == 1:
                    generated.write_text("created during first turn", encoding="utf-8")
            payload = {
                "id": "chatcmpl-cli-rollback",
                "object": "chat.completion",
                "created": 1,
                "model": "cli-test-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "mock assistant reply"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        commands = "\n".join([
            f"/workspace set {workspace}",
            "/rollback status",
            "first turn",
            "/rollback list",
            "/rollback",
            "/rollback status",
            "/rollback undo",
            "/session list",
            "/skill list",
            "/help",
            "/workspace unset",
            "/rollback status",
            "/exit",
            "",
        ])
        env = os.environ.copy()
        env.update({
            "SJTUCLAW_DATA_DIR": str(data_dir),
            "LLM_API_KEY": "cli-test-key",
            "LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "LLM_MODEL": "cli-test-model",
            "LLM_MAX_RETRIES": "0",
            "HEARTBEAT_ENABLED": "false",
            "COMPACT_IDLE_TTL_MINUTES": "0",
            "PYTHONUTF8": "1",
        })
        completed = subprocess.run(
            [sys.executable, "-m", "claw.cli.main", "chat"],
            input=commands,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, output
    assert "Workspace 已设置为" in output
    assert "Workspace 回退已启用" in output
    assert "可用回退点" in output
    assert "回退完成" in output
    assert "已撤销上一次回退" in output
    assert "SJTUClaw 可用指令" in output
    assert "Skill registry 未初始化" not in output
    assert "当前 session 未启用回退" in output

    # The unit-level rollback tests verify the intermediate deletion.  This
    # process-level flow verifies that the real CLI can then undo and restore
    # the first turn's filesystem side effect.
    assert observations["agent_requests"] == 1
    assert generated.read_text(encoding="utf-8") == "created during first turn"

    bindings = json.loads((data_dir / "workspace" / "bindings.json").read_text("utf-8"))
    assert bindings.get("default", {}).get("path") is None

    restored_session = SessionStore(data_dir / "sessions").get("default")
    contents = [message.content for message in restored_session.messages]
    assert "first turn" in contents


def test_cli_entry_help_uses_utf8_output():
    completed = subprocess.run(
        [sys.executable, "-m", "claw.cli.main", "--help"],
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=15,
        check=False,
    )
    output = completed.stdout.decode("utf-8")
    assert completed.returncode == 0
    assert "SJTUClaw — AI Agent" in output
