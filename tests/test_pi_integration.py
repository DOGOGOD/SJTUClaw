"""Contract tests for the Pi JSONL RPC integration."""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest
from fastapi import HTTPException

from claw.agent.events import FinalEvent, ToolCallEndEvent, ToolCallStartEvent
from claw.approval.manager import ApprovalStatus
from claw.config import LLMConfig
from claw.pi.client import PiAgentClient, PiRuntimeConfig, RuntimeAgentClient
from claw.session.store import SessionStore
from claw.tools.base import Tool, ToolRegistry, ToolResult


_FAKE_PI = r'''
import json, sys
def send(v):
    sys.stdout.write(json.dumps(v, ensure_ascii=False) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    command = json.loads(line)
    if command.get("type") == "prompt":
        send({"type":"response","id":command["id"],"command":"prompt","success":True})
        send({"type":"agent_start"})
        send({"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"中间说明"}})
        send({"type":"tool_execution_start","toolCallId":"call-1","toolName":"write","args":{"path":"x.txt"}})
        send({"type":"extension_ui_request","id":"approval-1","method":"confirm","title":"SJTUClaw 工具审批","message":json.dumps({"toolName":"write","input":{"path":"x.txt"}})})
    elif command.get("type") == "extension_ui_response":
        decision = "完成喵" if command.get("confirmed") is True else "拒绝喵"
        send({"type":"tool_execution_end","toolCallId":"call-1","toolName":"write","result":{"content":[{"type":"text","text":"written"}]},"isError":False})
        send({"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":decision}})
        send({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":decision}]}})
        send({"type":"agent_settled"})
'''

_CANCELLABLE_PI = r'''
import json, sys
def send(v):
    sys.stdout.write(json.dumps(v) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    command = json.loads(line)
    if command.get("type") == "prompt":
        send({"type":"response","id":command["id"],"command":"prompt","success":True})
        send({"type":"agent_start"})
    elif command.get("type") == "abort":
        send({"type":"response","id":command["id"],"command":"abort","success":True})
        send({"type":"agent_settled"})
'''

_SILENT_PI = "import time; time.sleep(30)\n"

_ERROR_PI = r'''
import json, sys
command = json.loads(sys.stdin.readline())
def send(v):
    sys.stdout.write(json.dumps(v) + "\n"); sys.stdout.flush()
send({"type":"response","id":command["id"],"command":"prompt","success":True})
send({"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"Connection error."}})
send({"type":"auto_retry_end","success":False,"attempt":3,"finalError":"Connection error."})
send({"type":"agent_settled"})
'''

_COMPACT_PI = r'''
import json, sys
command = json.loads(sys.stdin.readline())
sys.stdout.write(json.dumps({
    "type":"response", "id":command["id"], "command":"compact", "success":True,
    "data":{"summary":"native pi summary", "tokensBefore":1234}
}) + "\n")
sys.stdout.flush()
'''


def _runtime(tmp_path, script, *, timeout=10):
    path = tmp_path / "fake_pi.py"
    path.write_text(script, encoding="utf-8")
    return PiRuntimeConfig(
        command=(sys.executable, str(path)),
        cwd=tmp_path,
        session_dir=tmp_path / "pi-sessions",
        trust_tools=False,
        turn_timeout_s=timeout,
    )


def _client_and_store(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.save(store.create_session(session_id="pi-test"))
    return PiAgentClient(LLMConfig("", "https://api.openai.com/v1", "")), store


def test_pi_rpc_maps_events_approval_and_only_keeps_last_assistant(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setattr("claw.pi.client.load_pi_config", lambda: _runtime(tmp_path, _FAKE_PI))
    client, store = _client_and_store(tmp_path)
    events, approvals = [], []

    def approve(request):
        approvals.append(request)
        request.status = ApprovalStatus.APPROVED.value
        return request

    result = client.run_agent_turn(
        "pi-test", "写入文件", session_store=store,
        approval_handler=approve, event_callback=events.append,
    )

    assert result == "完成喵"
    assert "中间说明" not in result
    assert approvals[0].tool_name == "write"
    assert approvals[0].tool_args == {"path": "x.txt"}
    assert any(isinstance(event, ToolCallStartEvent) for event in events)
    assert any(isinstance(event, ToolCallEndEvent) for event in events)
    assert isinstance(events[-1], FinalEvent)
    assert [(message.role, message.content) for message in store.get("pi-test").messages] == [
        ("user", "写入文件"), ("assistant", "完成喵"),
    ]


def test_pi_cancel_sends_abort_without_waiting_for_more_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr("claw.pi.client.load_pi_config", lambda: _runtime(tmp_path, _CANCELLABLE_PI))
    client, store = _client_and_store(tmp_path)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()

    started = time.monotonic()
    result = client.run_agent_turn("pi-test", "等待", session_store=store, cancel_event=cancel)

    assert "用户终止" in result
    assert time.monotonic() - started < 3


def test_pi_timeout_is_enforced_while_process_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "claw.pi.client.load_pi_config",
        lambda: _runtime(tmp_path, _SILENT_PI, timeout=0.3),
    )
    client, store = _client_and_store(tmp_path)

    started = time.monotonic()
    result = client.run_agent_turn("pi-test", "卡住", session_store=store)

    assert "超过 0.3 秒" in result
    assert time.monotonic() - started < 3


def test_pi_model_error_is_not_reported_as_empty_success(tmp_path, monkeypatch):
    monkeypatch.setattr("claw.pi.client.load_pi_config", lambda: _runtime(tmp_path, _ERROR_PI))
    client, store = _client_and_store(tmp_path)

    result = client.run_agent_turn("pi-test", "失败", session_store=store)

    assert result == "Pi Agent 执行失败：Connection error."


def test_pi_manual_compact_uses_native_rpc_command(tmp_path, monkeypatch):
    monkeypatch.setattr("claw.pi.client.load_pi_config", lambda: _runtime(tmp_path, _COMPACT_PI))
    client, store = _client_and_store(tmp_path)

    result = client.compact_session("pi-test", session_store=store)

    assert "Pi session 已完成原生压缩" in result
    assert "1234 tokens" in result
    assert "native pi summary" in result


def test_pi_uses_session_bound_workspace_as_process_cwd(tmp_path, monkeypatch):
    configured = _runtime(tmp_path, _ERROR_PI)
    monkeypatch.setattr("claw.pi.client.load_pi_config", lambda: configured)
    client, store = _client_and_store(tmp_path)
    workspace = tmp_path / "bound-workspace"
    workspace.mkdir()
    observed = []

    class Context:
        def bound_workspace(self, session_id):
            assert session_id == "pi-test"
            return str(workspace)

    def fake_rpc(_command, config, *_args, **_kwargs):
        observed.append(config.cwd)
        return "ok"

    monkeypatch.setattr(client, "_run_rpc", fake_rpc)
    client.run_agent_turn("pi-test", "cwd", session_store=store, context_builder=Context())

    assert observed == [workspace.resolve()]


def test_new_pi_branch_receives_existing_sjtuclaw_history_once(tmp_path, monkeypatch):
    configured = _runtime(tmp_path, _ERROR_PI)
    monkeypatch.setattr("claw.pi.client.load_pi_config", lambda: configured)
    client, store = _client_and_store(tmp_path)
    session = store.get("pi-test")
    session.append_message("user", "先前问题")
    session.append_message("assistant", "先前回答")
    store.save(session)
    prompts = []

    def fake_rpc(_command, _config, prompt, **kwargs):
        prompts.append(prompt)
        kwargs["on_prompt_accepted"]()
        return "ok"

    monkeypatch.setattr(client, "_run_rpc", fake_rpc)
    client.run_agent_turn("pi-test", "继续", session_store=store)
    client.run_agent_turn("pi-test", "再继续", session_store=store)

    assert "sjtuclaw_session_handoff" in prompts[0]
    assert "先前问题" in prompts[0] and "先前回答" in prompts[0]
    assert prompts[1] == "再继续"
    metadata = store.get("pi-test").metadata
    assert metadata["pi_session_owner"] == "pi-test"
    assert metadata["pi_initialized_generation"] == metadata["pi_session_generation"]


def test_pi_unknown_dialog_is_cancelled_fail_closed():
    replies = []
    PiAgentClient._handle_ui_request(
        {"type": "extension_ui_request", "id": "x", "method": "input", "title": "Other"},
        replies.append,
        session_id="s",
        approval_handler=None,
        trust_tools=False,
    )
    assert replies == [{"type": "extension_ui_response", "id": "x", "cancelled": True}]


def test_clearing_session_rotates_pi_session_generation():
    from claw.session.models import Session

    session = Session(session_id="s", title="s", metadata={"pi_session_generation": "old"})
    session.clear()
    assert session.metadata["pi_session_generation"] != "old"
    assert len(session.metadata["pi_session_generation"]) == 32


def test_gateway_considers_pi_configured_without_auxiliary_llm(monkeypatch):
    from claw.gateway import server

    config = LLMConfig("", "https://api.openai.com/v1", "")
    monkeypatch.setattr(server, "create_agent_client", PiAgentClient)
    runtime = server.RuntimeLLMClient()
    runtime.set_config(config)

    assert runtime.configured is True
    assert callable(getattr(runtime, "run_agent_turn"))


def test_gateway_legacy_runtime_does_not_expose_full_turn(monkeypatch):
    from claw.gateway import server

    class Legacy:
        config = LLMConfig("key", "https://example.test/v1", "model")

    monkeypatch.setattr(server, "create_agent_client", lambda _config: Legacy())
    runtime = server.RuntimeLLMClient()
    runtime.set_config(Legacy.config)

    assert getattr(runtime, "run_agent_turn", None) is None


def test_apply_runtime_config_accepts_pi_without_legacy_credentials(monkeypatch):
    from claw.gateway import server

    settings = {
        "backend": "pi",
        "baseUrl": "https://api.openai.com/v1",
        "model": "",
        "contextWindow": 32000,
        "contextUsageRatio": 0.8,
        "maxOutputTokens": 4096,
        "consolidationRatio": 0.5,
    }
    configured = []
    stopped = []
    monkeypatch.setattr(server, "_llm_settings_payload", lambda: settings)
    monkeypatch.setattr(server, "setting_value", lambda *_args: "")
    monkeypatch.setattr(server._llm_client, "set_config", configured.append)
    monkeypatch.setattr(server._compaction_worker, "stop_idle_compaction", lambda: stopped.append(True))

    server._apply_llm_runtime_config()

    assert configured and configured[0].api_key == ""
    assert stopped == [True]


def test_settings_reject_invalid_pi_thinking_before_applying_runtime():
    from claw.gateway import server

    request = server.LLMSettingsRequest(
        backend="pi",
        baseUrl="",
        model="",
        contextWindow=32000,
        contextUsageRatio=0.8,
        maxOutputTokens=4096,
        consolidationRatio=0.5,
        piThinking="turbo",
    )

    with pytest.raises(HTTPException, match="Pi thinking level 无效") as exc_info:
        server.update_llm_settings(request)

    assert exc_info.value.status_code == 400


def test_pi_command_uses_native_prompt_and_only_appends_sjtu_context(tmp_path):
    prompt = tmp_path / "sjtu-prompt.md"
    prompt.write_text("SJTU context", encoding="utf-8")
    config = _runtime(tmp_path, _ERROR_PI)
    config = PiRuntimeConfig(**{**config.__dict__, "append_prompt_file": prompt})

    command = PiAgentClient._build_command(config, "session-id")

    assert "--system-prompt" not in command
    assert command.count("--append-system-prompt") == 1
    assert str(prompt) in command
    assert any(value.endswith("sjtuclaw_tools.ts") for value in command)


def test_pi_append_prompt_keeps_identity_memory_but_not_legacy_tool_contract(tmp_path):
    from claw.context.builder import ContextBuilder
    from claw.memory.store import MemoryStore

    memory = MemoryStore(tmp_path / "memory")
    memory.add(
        content="用户偏好深色主题",
        category="user_preference",
        tags=[],
        importance=4,
    )
    builder = ContextBuilder("SJTU system", "SJTU soul", memory, workspace_path=str(tmp_path))

    prompt = builder.build_pi_append_prompt("session-a")

    assert "SJTU system" in prompt and "SJTU soul" in prompt
    assert "用户偏好深色主题" in prompt
    assert "Pi 原生 system prompt" in prompt
    assert "find_files" not in prompt
    assert "edit_file" not in prompt


def test_pi_runtime_manifest_bridges_sjtu_tools_but_not_native_equivalents(tmp_path):
    registry = ToolRegistry()
    registry.register(Tool("recall", "Recall memory", {"type": "object", "properties": {}}, lambda _args: ToolResult(True, "ok")))
    registry.register(Tool("read_file", "Legacy reader", {"type": "object", "properties": {}}, lambda _args: ToolResult(True, "ok")))
    config = _runtime(tmp_path, _ERROR_PI)

    files = PiAgentClient._write_runtime_files(
        config, "pi-session", session_id="s", context_builder=None, tool_registry=registry,
    )
    manifest = json.loads(files["tools"].read_text(encoding="utf-8"))

    assert [tool["name"] for tool in manifest["tools"]] == ["recall"]
    assert manifest["tools"][0]["description"] == "Recall memory"


def test_pi_host_tool_bridge_executes_registry_tool():
    registry = ToolRegistry()
    registry.register(Tool(
        "echo_host", "Echo", {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }, lambda args: ToolResult(True, args["value"]),
    ))
    replies = []
    payload = json.dumps({"toolName": "echo_host", "input": {"value": "桥接成功"}})

    PiAgentClient._handle_ui_request(
        {"type": "extension_ui_request", "id": "bridge", "method": "input",
         "title": "SJTUClaw 工具桥接", "placeholder": payload},
        replies.append, session_id="s", approval_handler=None, tool_registry=registry,
    )

    response = json.loads(replies[0]["value"])
    assert response == {"ok": True, "result": "桥接成功"}


def test_pi_host_tool_bridge_fails_closed_for_unapproved_write():
    executed = []
    registry = ToolRegistry()
    registry.register(Tool(
        "host_write", "Write", {"type": "object", "properties": {}},
        lambda _args: executed.append(True) or ToolResult(True, "written"),
        safety_level="write",
    ))

    response = json.loads(PiAgentClient._execute_host_tool(
        json.dumps({"toolName": "host_write", "input": {}}),
        session_id="s", tool_registry=registry, approval_handler=None,
        trust_tools=False, auto_mode=False, unlimited_mode=False,
    ))

    assert response["ok"] is False
    assert executed == []


def test_pi_host_tool_bridge_rejects_spoofed_token():
    registry = ToolRegistry()
    registry.register(Tool(
        "host_read", "Read", {"type": "object", "properties": {}},
        lambda _args: ToolResult(True, "secret"),
    ))

    response = json.loads(PiAgentClient._execute_host_tool(
        json.dumps({"token": "wrong", "toolName": "host_read", "input": {}}),
        session_id="s", tool_registry=registry, approval_handler=None,
        trust_tools=False, auto_mode=False, unlimited_mode=False,
        bridge_token="expected",
    ))

    assert response == {"ok": False, "result": "SJTUClaw 工具桥接认证失败。"}


def test_pi_slash_command_switches_backend_through_runtime_callback(tmp_path):
    from claw.cli.commands import RuntimeState, handle_command, is_command
    from claw.memory.store import MemoryStore

    calls = []
    state = RuntimeState(
        session_store=SessionStore(tmp_path / "sessions"),
        memory_store=MemoryStore(tmp_path / "memory"),
        llm_client=object(),
        current_session_id="s",
        backend_switcher=lambda target: calls.append(target) or f"switched:{target}",
    )

    assert is_command("/pi") is True
    assert handle_command("/pi", state) == "switched:pi"
    assert handle_command("/pi status", state) == "switched:status"
    assert handle_command("/pi off", state) == "switched:sjtuclaw"
    assert calls == ["pi", "status", "sjtuclaw"]


def test_gateway_pi_command_updates_runtime_backend(monkeypatch):
    from claw.gateway import server
    import claw.pi as pi_module

    settings = {"AGENT_BACKEND": "sjtuclaw"}
    applied = []
    monkeypatch.setattr(
        server,
        "setting_value",
        lambda name, default="": settings.get(name, default),
    )
    monkeypatch.setattr(server, "load_runtime_settings_raw", lambda: dict(settings))
    monkeypatch.setattr(server, "replace_runtime_settings_raw", lambda values: settings.update(values))
    monkeypatch.setattr(server, "update_runtime_settings", lambda values: settings.update(values))
    monkeypatch.setattr(server, "_apply_llm_runtime_config", lambda: applied.append(settings["AGENT_BACKEND"]))
    monkeypatch.setattr(server, "_session_turn_active", lambda _sid: False)
    monkeypatch.setattr(pi_module, "load_pi_config", lambda: object())

    result = server._execute_slash_command("/pi", "pi-command-test")

    assert "已接入 Pi" in result
    assert settings["AGENT_BACKEND"] == "pi"
    assert applied == ["pi"]


def test_runtime_agent_router_switches_background_and_foreground_capability(monkeypatch):
    class Legacy:
        config = LLMConfig("key", "https://example.test/v1", "legacy")

        def chat(self, *_args, **_kwargs):
            return "legacy-chat"

        def chat_with_tools(self, *_args, **_kwargs):
            return "legacy-tools"

    legacy = Legacy()
    monkeypatch.setattr("claw.pi.client.create_agent_client", lambda _config: legacy)
    router = RuntimeAgentClient(legacy.config)
    assert router.chat([]) == "legacy-chat"
    assert getattr(router, "run_agent_turn", None) is None

    pi = PiAgentClient(LLMConfig("", "https://example.test/v1", ""))
    router.set_client(pi)
    assert callable(getattr(router, "run_agent_turn"))
    assert callable(getattr(router, "compact_session"))


def test_compact_command_routes_to_pi_native_compaction(tmp_path):
    from claw.cli.commands import RuntimeState, handle_command
    from claw.memory.store import MemoryStore

    class PiLike:
        def compact_session(self, session_id, *, session_store):
            assert session_id == "s"
            assert session_store is store
            return "native compact ok"

    store = SessionStore(tmp_path / "sessions")
    store.save(store.create_session(session_id="s"))
    state = RuntimeState(
        session_store=store,
        memory_store=MemoryStore(tmp_path / "memory"),
        llm_client=PiLike(),
        current_session_id="s",
    )

    assert handle_command("/compact", state) == "native compact ok"
