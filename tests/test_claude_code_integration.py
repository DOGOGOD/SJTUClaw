"""Contract tests for the Claude Code stream-json integration."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from claw.agent.events import FinalEvent, ToolCallEndEvent, ToolCallStartEvent
from claw.approval.manager import ApprovalRequest, ApprovalStatus
from claw.claude.client import (
    _CLAUDE_APPROVAL_MATCHER,
    ClaudeCodeAgentClient,
    ClaudeCodeRuntimeConfig,
    _ClaudeApprovalBridge,
    _claude_tool_requires_approval,
    resolve_claude_code_command,
)
from claw.claude.mcp_server import HostToolMcpServer
from claw.config import LLMConfig
from claw.context.builder import ContextBuilder
from claw.memory.store import MemoryStore
from claw.pi.client import RuntimeAgentClient, get_session_backend, set_session_backend
from claw.session.store import SessionStore
from claw.tools.base import Tool, ToolRegistry, ToolResult


_FAKE_CLAUDE = r'''
import json, sys

capture = sys.argv[1]
args = sys.argv[2:]
prompt = sys.stdin.buffer.read().decode("utf-8")
settings = None
if "--settings" in args:
    with open(args[args.index("--settings") + 1], encoding="utf-8") as fh:
        settings = json.load(fh)
mcp_config = None
tool_manifest = None
if "--mcp-config" in args:
    with open(args[args.index("--mcp-config") + 1], encoding="utf-8") as fh:
        mcp_config = json.load(fh)
    server = mcp_config["mcpServers"]["sjtuclaw_host_tools"]
    manifest_flag = server["args"].index("--manifest")
    with open(server["args"][manifest_flag + 1], encoding="utf-8") as fh:
        tool_manifest = json.load(fh)
with open(capture, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(
        {
            "args": args,
            "prompt": prompt,
            "settings": settings,
            "mcp_config": mcp_config,
            "tool_manifest": tool_manifest,
        },
        ensure_ascii=False,
    ) + "\n")

session_id = ""
for flag in ("--session-id", "--resume"):
    if flag in args:
        session_id = args[args.index(flag) + 1]

def send(value):
    sys.stdout.buffer.write(
        (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    )
    sys.stdout.buffer.flush()

send({
    "type": "system",
    "subtype": "init",
    "session_id": session_id,
    "model": "fake-claude",
    "tools": ["Read", "Write"],
})
send({
    "type": "assistant",
    "session_id": session_id,
    "message": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "先处理文件"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "Write",
                "input": {"file_path": "x.txt", "content": "ok"},
            },
        ],
    },
})
send({
    "type": "user",
    "session_id": session_id,
    "message": {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "File written",
                "is_error": False,
            }
        ],
    },
})
send({
    "type": "result",
    "subtype": "success",
    "session_id": session_id,
    "is_error": False,
    "result": "Claude 完成",
})
'''

_SILENT_CLAUDE = "import time; time.sleep(30)\n"
_CHILD_CLAUDE = r'''
import subprocess, sys, time

capture = sys.argv[1]
marker = capture + ".child"
ready = capture + ".ready"
child_code = (
    "import pathlib, time; "
    "time.sleep(0.8); "
    f"pathlib.Path({marker!r}).write_text('survived', encoding='utf-8')"
)
subprocess.Popen([sys.executable, "-c", child_code])
with open(ready, "w", encoding="utf-8") as fh:
    fh.write("ready")
time.sleep(30)
'''


def _runtime(tmp_path: Path, script: str, *, timeout: float = 10) -> tuple[ClaudeCodeRuntimeConfig, Path]:
    cli = tmp_path / "fake_claude.py"
    cli.write_text(script, encoding="utf-8")
    capture = tmp_path / "calls.jsonl"
    return (
        ClaudeCodeRuntimeConfig(
            command=(sys.executable, str(cli), str(capture)),
            cwd=tmp_path,
            permission_mode="default",
            turn_timeout_s=timeout,
        ),
        capture,
    )


def _client_and_store(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="claude-test")
    client = ClaudeCodeAgentClient(
        LLMConfig("", "https://api.openai.com/v1", "")
    )
    return client, store


def test_claude_stream_maps_tools_and_resumes_same_session(tmp_path, monkeypatch):
    runtime, capture = _runtime(tmp_path, _FAKE_CLAUDE)
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: runtime,
    )
    client, store = _client_and_store(tmp_path)
    events = []

    assert client.run_agent_turn(
        "claude-test",
        "第一次",
        session_store=store,
        event_callback=events.append,
    ) == "Claude 完成"
    assert client.run_agent_turn(
        "claude-test",
        "第二次",
        session_store=store,
    ) == "Claude 完成"

    calls = [
        json.loads(line)
        for line in capture.read_text(encoding="utf-8").splitlines()
    ]
    assert "--session-id" in calls[0]["args"]
    assert "--resume" not in calls[0]["args"]
    assert "--resume" in calls[1]["args"]
    first_id = calls[0]["args"][calls[0]["args"].index("--session-id") + 1]
    resumed_id = calls[1]["args"][calls[1]["args"].index("--resume") + 1]
    assert resumed_id == first_id
    assert calls[0]["prompt"] == "第一次"
    assert calls[1]["prompt"] == "第二次"
    assert "--settings" in calls[0]["args"]
    assert "--system-prompt" not in calls[0]["args"]
    settings = calls[0]["settings"]
    assert settings["disableAllHooks"] is False
    assert settings["sandbox"]["autoAllowBashIfSandboxed"] is False
    assert "PreToolUse" in settings["hooks"]
    assert "PermissionRequest" not in settings["hooks"]
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert "-EncodedCommand" in hook["command"]

    tool_events = [
        event
        for event in events
        if isinstance(event, (ToolCallStartEvent, ToolCallEndEvent))
    ]
    assert [event.call_id for event in tool_events] == ["toolu_1", "toolu_1"]
    assert isinstance(events[-1], FinalEvent)
    messages = store.get("claude-test").messages
    assert [(message.role, message.content) for message in messages[:4]] == [
        ("user", "第一次"),
        ("assistant", ""),
        ("tool", "File written"),
        ("assistant", "Claude 完成"),
    ]
    assert messages[1].tool_calls[0]["function"]["name"] == "Write"


def test_claude_new_branch_receives_authoritative_history_once(tmp_path, monkeypatch):
    runtime, capture = _runtime(tmp_path, _FAKE_CLAUDE)
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: runtime,
    )
    client, store = _client_and_store(tmp_path)
    session = store.get("claude-test")
    session.append_message("user", "先前问题")
    session.append_message("assistant", "先前回答")
    store.save(session)

    client.run_agent_turn("claude-test", "继续", session_store=store)
    client.run_agent_turn("claude-test", "再继续", session_store=store)

    calls = [
        json.loads(line)
        for line in capture.read_text(encoding="utf-8").splitlines()
    ]
    assert "sjtuclaw_session_handoff" in calls[0]["prompt"]
    assert "先前问题" in calls[0]["prompt"]
    assert "先前回答" in calls[0]["prompt"]
    assert calls[1]["prompt"] == "再继续"


def test_returning_to_claude_rotates_native_session(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="switch")
    set_session_backend(store, "switch", "claude")
    old_generation = store.get("switch").metadata["claude_session_generation"]

    set_session_backend(store, "switch", "sjtuclaw")
    set_session_backend(store, "switch", "claude")

    assert get_session_backend(store, "switch") == "claude"
    assert (
        store.get("switch").metadata["claude_session_generation"]
        != old_generation
    )


def test_claude_auto_discovery_checks_known_locations(tmp_path, monkeypatch):
    candidate = tmp_path / "claude"
    candidate.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "claw.claude.client.setting_value",
        lambda _name, default="": default,
    )
    monkeypatch.setattr("claw.claude.client.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "claw.claude.client._candidate_commands",
        lambda: [candidate],
    )

    assert resolve_claude_code_command() == (str(candidate.resolve()),)


def test_claude_keeps_native_prompt_and_workspace_is_only_cwd(tmp_path):
    append_prompt = tmp_path / "append.md"
    append_prompt.write_text("SJTUClaw context", encoding="utf-8")
    config = ClaudeCodeRuntimeConfig(
        command=("claude",),
        cwd=tmp_path,
        append_prompt_file=append_prompt,
    )
    command = ClaudeCodeAgentClient._build_command(
        config,
        "00000000-0000-0000-0000-000000000001",
        resume=False,
    )

    assert "--append-system-prompt-file" in command
    assert "--system-prompt" not in command

    memory = MemoryStore(tmp_path / "memory")
    builder = ContextBuilder(
        "SJTU system",
        "SJTU soul",
        memory,
        workspace_path=str(tmp_path),
    )
    prompt = builder.build_claude_code_append_prompt("session-a")

    assert "工作区作为启动目录" in prompt
    assert "不构成 SJTUClaw 文件访问边界" in prompt
    assert "不对 Claude Code 原生工具施加 workspace 越界限制" in prompt
    assert "MCP 宿主工具桥接" in prompt


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("WebSearch", {"query": "SJTUClaw"}),
        ("WebFetch", {"url": "https://example.com"}),
        ("Agent", {"prompt": "inspect"}),
        ("Bash", {"command": "git status"}),
        ("Bash", {"command": "rg TODO . | head"}),
        ("PowerShell", {"command": "Get-ChildItem -Force"}),
        ("mcp__github__search_issues", {"query": "bug"}),
        ("mcp__github__list_issues", {}),
        ("mcp__server__web_search", {"query": "bug"}),
    ],
)
def test_claude_read_and_search_tools_are_not_dangerous(tool_name, tool_input):
    assert _claude_tool_requires_approval(tool_name, tool_input) is False


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Write", {"file_path": "x.txt", "content": "x"}),
        ("Edit", {"file_path": "x.txt", "old_string": "a", "new_string": "b"}),
        ("Bash", {"command": "rm important.txt"}),
        ("Bash", {"command": "git commit -am update"}),
        ("PowerShell", {"command": "Remove-Item important.txt"}),
        ("Bash", {"command": "echo $(rm important.txt)"}),
        ("Bash", {"command": "cat <(touch important.txt)"}),
        ("Bash", {"command": "git diff --output=important.txt"}),
        ("Bash", {"command": "GIT_EXTERNAL_DIFF=rm git diff --ext-diff"}),
        ("Bash", {"command": "git grep --open-files-in-pager=rm TODO"}),
        ("Bash", {"command": "rg --pre=touch TODO ."}),
        ("mcp__github__create_issue", {"title": "bug"}),
        ("mcp__server__delete_record", {"id": "1"}),
        ("mcp__server__getAndDeleteRecord", {"id": "1"}),
        ("mcp__server__searchAndArchive", {"id": "1"}),
        ("mcp__server__unknown_operation", {}),
    ],
)
def test_claude_state_changing_tools_are_dangerous(tool_name, tool_input):
    assert _claude_tool_requires_approval(tool_name, tool_input) is True


def test_claude_web_search_is_explicitly_allowed_without_sjtu_approval(tmp_path):
    approvals = []
    bridge = _ClaudeApprovalBridge(
        "session-search",
        lambda request: approvals.append(request),
        relay_root=tmp_path,
    )
    bridge.start()
    try:
        allowed, _reason = bridge.decide(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "WebSearch",
                "tool_input": {"query": "Claude Code"},
            }
        )
    finally:
        bridge.close()

    assert allowed is True
    assert approvals == []
    assert "WebSearch" in _CLAUDE_APPROVAL_MATCHER
    assert "WebFetch" in _CLAUDE_APPROVAL_MATCHER


def test_claude_dangerous_tool_uses_sjtuclaw_approval_once():
    seen: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ApprovalRequest:
        seen.append(request)
        request.status = ApprovalStatus.APPROVED.value
        return request

    bridge = _ClaudeApprovalBridge("session-approval", approve)
    bridge.start()
    payload = {
        "session_id": "native-session",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "Remove-Item important.txt"},
    }
    try:
        allowed, _reason = bridge.decide(payload)
    finally:
        bridge.close()

    assert len(seen) == 1
    assert seen[0].session_id == "session-approval"
    assert seen[0].tool_name == "Claude Code / Bash"
    assert allowed is True


def test_claude_auto_mode_approves_native_mutating_tool_without_prompt():
    approvals = []
    bridge = _ClaudeApprovalBridge(
        "session-auto",
        lambda request: approvals.append(request),
        auto_mode=True,
    )
    bridge.start()
    try:
        allowed, reason = bridge.decide(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "x.txt", "content": "ok"},
            }
        )
    finally:
        bridge.close()

    assert allowed is True
    assert "AUTO" in reason
    assert approvals == []


def test_claude_auto_mode_does_not_bypass_unlimited_approval():
    approvals = []

    def reject(request: ApprovalRequest) -> ApprovalRequest:
        approvals.append(request)
        request.status = ApprovalStatus.REJECTED.value
        return request

    bridge = _ClaudeApprovalBridge(
        "session-auto-unlimited",
        reject,
        auto_mode=True,
        unlimited_mode=True,
    )
    bridge.start()
    try:
        allowed, _reason = bridge.decide(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "Remove-Item important.txt"},
            }
        )
    finally:
        bridge.close()

    assert allowed is False
    assert len(approvals) == 1


def test_claude_dangerous_tool_fails_closed_without_approval_channel():
    bridge = _ClaudeApprovalBridge("session-no-approval", None)
    bridge.start()
    try:
        allowed, _reason = bridge.decide(
            {
                "session_id": "native-session",
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "important.txt", "content": "x"},
            }
        )
    finally:
        bridge.close()

    assert allowed is False


def test_claude_command_hook_relays_approval_and_fails_closed():
    def approve(request: ApprovalRequest) -> ApprovalRequest:
        request.status = ApprovalStatus.APPROVED.value
        return request

    payload = json.dumps(
        {
            "session_id": "native-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "approved.txt", "content": "ok"},
        }
    )
    bridge = _ClaudeApprovalBridge("session-command-hook", approve)
    bridge.start()
    command = ClaudeCodeAgentClient._approval_hook_command(bridge)
    result: dict[str, subprocess.CompletedProcess] = {}

    def invoke_hook():
        result["relayed"] = subprocess.run(
            command,
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=True,
            timeout=10,
        )

    hook_thread = threading.Thread(target=invoke_hook, daemon=True)
    hook_thread.start()
    deadline = time.monotonic() + 10
    while hook_thread.is_alive() and time.monotonic() < deadline:
        bridge.process_pending()
        time.sleep(0.02)
    hook_thread.join(timeout=1)
    relayed = result["relayed"]
    bridge.close()
    unavailable = subprocess.run(
        command,
        input=payload,
        text=True,
        encoding="utf-8",
        capture_output=True,
        shell=True,
        timeout=10,
    )

    assert relayed.returncode == 0
    assert (
        json.loads(relayed.stdout)["hookSpecificOutput"]["permissionDecision"]
        == "allow"
    )
    assert unavailable.returncode == 2
    assert "tool blocked" in unavailable.stderr


def test_claude_runtime_exposes_sjtu_tools_but_keeps_native_equivalents_native(
    tmp_path,
    monkeypatch,
):
    runtime, capture = _runtime(tmp_path, _FAKE_CLAUDE)
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: runtime,
    )
    registry = ToolRegistry()
    schema = {"type": "object", "properties": {}}
    registry.register(
        Tool("recall", "Recall memory", schema, lambda _args: ToolResult(True, "ok"))
    )
    registry.register(
        Tool("cron", "Manage schedules", schema, lambda _args: ToolResult(True, "ok"))
    )
    registry.register(
        Tool("read_file", "Native equivalent", schema, lambda _args: ToolResult(True, "ok"))
    )
    client, store = _client_and_store(tmp_path)

    assert client.run_agent_turn(
        "claude-test",
        "使用记忆",
        session_store=store,
        tool_registry=registry,
    ) == "Claude 完成"

    call = json.loads(capture.read_text(encoding="utf-8").splitlines()[0])
    assert "--mcp-config" in call["args"]
    assert "--strict-mcp-config" not in call["args"]
    assert sorted(tool["name"] for tool in call["tool_manifest"]["tools"]) == [
        "cron",
        "recall",
    ]
    server = call["mcp_config"]["mcpServers"]["sjtuclaw_host_tools"]
    assert server["type"] == "stdio"
    assert "--manifest" in server["args"]
    assert "--relay-dir" in server["args"]


def _call_mcp_tool(server, bridge, name, arguments):
    result = {}

    def invoke():
        result["value"] = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while worker.is_alive() and time.monotonic() < deadline:
        bridge.process_pending()
        time.sleep(0.01)
    worker.join(timeout=1)
    assert not worker.is_alive()
    return result["value"]["result"]


def test_claude_mcp_bridge_executes_read_only_tool_without_approval(tmp_path):
    registry = ToolRegistry()
    registry.register(
        Tool(
            "recall",
            "Recall memory",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            lambda args: ToolResult(True, f"memory:{args['query']}"),
        )
    )
    host_tools = registry.list_compact_definitions()
    approvals = []
    bridge = _ClaudeApprovalBridge(
        "session-mcp",
        lambda request: approvals.append(request),
        relay_root=tmp_path,
        tool_registry=registry,
        host_tools=host_tools,
    )
    bridge.start()
    server = HostToolMcpServer(host_tools, bridge.relay_dir, bridge.token)
    try:
        response = _call_mcp_tool(server, bridge, "recall", {"query": "偏好"})
    finally:
        bridge.close()

    assert response["isError"] is False
    assert response["content"][0]["text"] == "memory:偏好"
    assert approvals == []


def test_claude_mcp_bridge_requires_approval_for_state_change(tmp_path):
    executed = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            "remember",
            "Remember fact",
            {"type": "object", "properties": {}},
            lambda _args: executed.append(True) or ToolResult(True, "saved"),
            safety_level="write",
        )
    )
    host_tools = registry.list_compact_definitions()
    approvals = []

    def reject(request):
        approvals.append(request)
        request.status = ApprovalStatus.REJECTED.value
        return request

    bridge = _ClaudeApprovalBridge(
        "session-mcp-write",
        reject,
        relay_root=tmp_path,
        tool_registry=registry,
        host_tools=host_tools,
    )
    bridge.start()
    server = HostToolMcpServer(host_tools, bridge.relay_dir, bridge.token)
    try:
        response = _call_mcp_tool(server, bridge, "remember", {})
    finally:
        bridge.close()

    assert response["isError"] is True
    assert approvals[0].tool_name == "remember"
    assert executed == []


def test_claude_mcp_stdio_protocol_lists_host_tools(tmp_path):
    manifest = tmp_path / "tools.json"
    relay = tmp_path / "relay"
    relay.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "tools": [
                    {
                        "name": "recall",
                        "description": "Recall memory",
                        "parameters": {"type": "object", "properties": {}},
                        "safety_level": "read_only",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    messages = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                }
            ),
            "",
        ]
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "claw.claude.mcp_server",
            "--manifest",
            str(manifest),
            "--relay-dir",
            str(relay),
            "--token",
            "test-token",
        ],
        input=messages,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
    )
    responses = [
        json.loads(line) for line in completed.stdout.splitlines() if line.strip()
    ]

    assert completed.returncode == 0
    assert responses[0]["result"]["serverInfo"]["name"] == "SJTUClaw Host Tools"
    assert responses[1]["result"]["tools"][0]["name"] == "recall"
    assert responses[1]["result"]["tools"][0]["annotations"]["readOnlyHint"] is True


def test_claude_mcp_marks_mixed_cron_tool_as_potentially_mutating():
    tool = HostToolMcpServer._mcp_tool(
        {
            "name": "cron",
            "description": "Manage schedules",
            "parameters": {"type": "object", "properties": {}},
            "safety_level": "read_only",
        }
    )

    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is True


def test_claude_fast_exit_does_not_drop_result_event(tmp_path, monkeypatch):
    runtime, _capture = _runtime(tmp_path, _FAKE_CLAUDE)
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: runtime,
    )
    client, store = _client_and_store(tmp_path)
    collect_stdout = client._collect_stdout

    def delayed_collect(proc, output):
        time.sleep(0.35)
        collect_stdout(proc, output)

    monkeypatch.setattr(client, "_collect_stdout", delayed_collect)

    assert client.run_agent_turn(
        "claude-test",
        "快速完成",
        session_store=store,
    ) == "Claude 完成"


def test_claude_cancel_terminates_silent_process(tmp_path, monkeypatch):
    runtime, _capture = _runtime(tmp_path, _SILENT_CLAUDE)
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: runtime,
    )
    client, store = _client_and_store(tmp_path)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()

    started = time.monotonic()
    result = client.run_agent_turn(
        "claude-test",
        "等待",
        session_store=store,
        cancel_event=cancel,
    )

    assert "用户终止" in result
    assert time.monotonic() - started < 3


def test_claude_cancel_terminates_child_process_tree(tmp_path, monkeypatch):
    runtime, capture = _runtime(tmp_path, _CHILD_CLAUDE)
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: runtime,
    )
    client, store = _client_and_store(tmp_path)
    cancel = threading.Event()
    ready = Path(str(capture) + ".ready")
    marker = Path(str(capture) + ".child")

    def cancel_after_child_starts():
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        cancel.set()

    threading.Thread(target=cancel_after_child_starts, daemon=True).start()
    result = client.run_agent_turn(
        "claude-test",
        "启动子进程后停止",
        session_store=store,
        cancel_event=cancel,
    )
    time.sleep(1)

    assert ready.exists()
    assert "用户终止" in result
    assert not marker.exists()


@pytest.mark.parametrize("permission_mode", ["manual", "bypassPermissions"])
def test_claude_settings_reject_unsafe_or_unsupported_permission_modes(
    permission_mode,
):
    from fastapi import HTTPException

    from claw.gateway import server

    request = server.AgentSettingsRequest(
        backend="claude",
        claudePermissionMode=permission_mode,
    )

    with pytest.raises(HTTPException, match="permission mode 无效"):
        server.update_agent_settings(request)


def test_runtime_router_dispatches_claude_session(tmp_path):
    class Claude:
        def run_agent_turn(self, session_id, user_message, **_kwargs):
            return f"claude:{session_id}:{user_message}"

    router = RuntimeAgentClient(
        LLMConfig("key", "https://example.test/v1", "legacy")
    )
    router._claude_client = Claude()
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="route-claude")
    set_session_backend(store, "route-claude", "claude")

    assert router.run_agent_turn(
        "route-claude",
        "hello",
        session_store=store,
    ) == "claude:route-claude:hello"


def test_claude_slash_command_switches_only_current_session(tmp_path, monkeypatch):
    from claw.gateway import server
    import claw.claude as claude_module

    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="claude-a")
    store.create_session(session_id="claude-b")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_session_turn_active", lambda _sid: False)
    monkeypatch.setattr(
        claude_module,
        "load_claude_code_config",
        lambda: ClaudeCodeRuntimeConfig(
            command=("claude",),
            cwd=tmp_path,
        ),
    )

    info = server._execute_slash_command("/claude", "claude-a")
    assert "Claude Code 后端" in info
    assert get_session_backend(store, "claude-a") == "sjtuclaw"

    result = server._execute_slash_command("/claude on", "claude-a")
    assert "已接入 Claude Code" in result
    assert get_session_backend(store, "claude-a") == "claude"
    assert get_session_backend(store, "claude-b") == "sjtuclaw"
    payload = server.get_messages("claude-a")
    assert payload["agentBackend"] == "claude"
    assert payload["piMode"] is False
