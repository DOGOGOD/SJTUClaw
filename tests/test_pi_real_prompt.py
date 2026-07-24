"""Opt-in real Pi process test using a local, non-network model stub."""

from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claw.config import LLMConfig
from claw.pi.client import PiAgentClient, PiRuntimeConfig
from claw.tools.base import Tool, ToolRegistry, ToolResult


@pytest.mark.skipif(
    os.getenv("SJTUCLAW_RUN_PI_INTEGRATION") != "1",
    reason="set SJTUCLAW_RUN_PI_INTEGRATION=1 to launch the adjacent Pi build",
)
def test_real_pi_request_keeps_native_tool_prompt_and_adds_sjtu_tools(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cli = root.parent / "pi" / "packages" / "coding-agent" / "dist" / "cli.js"
    node = os.getenv("PI_NODE_PATH") or shutil.which("node")
    if not node or not cli.is_file():
        pytest.skip("adjacent Pi build or Node.js is unavailable")

    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(length)))
            if len(requests) == 1:
                chunks = [
                    {
                        "id": "local-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "verification-model",
                        "choices": [{"index": 0, "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-recall",
                                "type": "function",
                                "function": {"name": "recall", "arguments": "{\"query\":\"theme\"}"},
                            }],
                        }, "finish_reason": None}],
                    },
                    {
                        "id": "local-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "verification-model",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    },
                ]
            elif len(requests) == 2:
                chunks = [
                    {
                        "id": "local-test-read",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "verification-model",
                        "choices": [{"index": 0, "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-read",
                                "type": "function",
                                "function": {"name": "read", "arguments": "{\"path\":\"README.md\",\"limit\":2}"},
                            }],
                        }, "finish_reason": None}],
                    },
                    {
                        "id": "local-test-read",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "verification-model",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                    },
                ]
            else:
                chunks = [
                    {
                        "id": "local-test-2",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "verification-model",
                        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
                    },
                    {
                        "id": "local-test-2",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "verification-model",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        prompt_file = tmp_path / "append.md"
        prompt_file.write_text("SJTUCLAW_PROMPT_MARKER", encoding="utf-8")
        manifest = tmp_path / "tools.json"
        manifest.write_text(json.dumps({
            "version": 1,
            "tools": [{
                "name": "recall",
                "description": "Recall durable SJTUClaw memory",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "safety_level": "read_only",
            }],
        }), encoding="utf-8")
        llm = LLMConfig(
            api_key="local-only",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="verification-model",
        )
        client = PiAgentClient(llm)
        registry = ToolRegistry()
        registry.register(Tool(
            "recall",
            "Recall durable SJTUClaw memory",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            lambda args: ToolResult(True, f"memory-hit:{args['query']}"),
        ))
        config = PiRuntimeConfig(
            command=(node, str(cli)),
            cwd=root,
            session_dir=tmp_path / "sessions",
            provider="sjtuclaw",
            model="verification-model",
            append_prompt_file=prompt_file,
            tool_manifest_file=manifest,
            bridge_token="local-bridge-token",
            turn_timeout_s=20,
        )

        result = client._run_rpc(
            client._build_command(config, "prompt-verification"),
            config,
            "reply with ok",
            media=[],
            session_id="verification",
            approval_handler=None,
            tool_registry=registry,
            auto_mode=False,
            unlimited_mode=False,
            event_callback=None,
            cancel_event=None,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result == "ok"
    request = requests[0]
    system_text = "\n".join(
        str(message.get("content", ""))
        for message in request["messages"]
        if message.get("role") == "system"
    )
    tool_names = {
        tool["function"]["name"]
        for tool in request["tools"]
    }
    assert "Available tools:" in system_text
    assert "- read: Read file contents" in system_text
    assert "- recall: Recall durable SJTUClaw memory" in system_text
    assert "Use recall before answering questions" in system_text
    assert "SJTUCLAW_PROMPT_MARKER" in system_text
    assert {"read", "bash", "edit", "write", "recall"} <= tool_names
    assert len(requests) == 3
    assert any(
        message.get("role") == "tool" and "memory-hit:theme" in str(message.get("content"))
        for message in requests[1]["messages"]
    )
    # No approval handler is installed in this test. A guarded read would be
    # rejected by the permission extension instead of returning file content.
    assert any(
        message.get("role") == "tool" and "# SJTUClaw" in str(message.get("content"))
        for message in requests[2]["messages"]
    )
