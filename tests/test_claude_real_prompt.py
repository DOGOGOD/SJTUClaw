"""Opt-in smoke test against the user's installed Claude Code."""

from __future__ import annotations

import os

import pytest

from claw.agent.events import ToolCallStartEvent
from claw.approval.manager import ApprovalStatus
from claw.claude.client import (
    ClaudeCodeAgentClient,
    ClaudeCodeRuntimeConfig,
    resolve_claude_code_command,
)
from claw.config import LLMConfig
from claw.session.store import SessionStore
from claw.tools.base import Tool, ToolRegistry, ToolResult


@pytest.mark.skipif(
    os.getenv("SJTUCLAW_RUN_CLAUDE_INTEGRATION") != "1",
    reason="set SJTUCLAW_RUN_CLAUDE_INTEGRATION=1 to launch installed Claude Code",
)
def test_real_claude_discovers_and_calls_sjtuclaw_recall(tmp_path, monkeypatch):
    command = resolve_claude_code_command()
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: ClaudeCodeRuntimeConfig(
            command=command,
            cwd=tmp_path,
            permission_mode="default",
            trust_tools=False,
            turn_timeout_s=180,
        ),
    )

    class Context:
        @staticmethod
        def bound_workspace(_session_id):
            return str(tmp_path)

        @staticmethod
        def build_claude_code_append_prompt(_session_id):
            return (
                "SJTUCLAW_REAL_PROMPT_MARKER. Preserve all Claude Code native "
                "instructions and use additionally supplied SJTUClaw MCP tools."
            )

    executed = []
    approvals = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            "recall",
            "Recall durable SJTUClaw memory matching a query.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            lambda args: (
                executed.append(args)
                or ToolResult(True, "SJTUCLAW_REAL_RECALL_RESULT")
            ),
            safety_level="read_only",
        )
    )
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="claude-real")
    client = ClaudeCodeAgentClient(LLMConfig("", "", ""))
    events = []

    result = client.run_agent_turn(
        "claude-real",
        (
            "You must call the SJTUClaw recall MCP tool exactly once with "
            'query "real-smoke". Do not use any other tool. Then answer with '
            "the exact tool result."
        ),
        session_store=store,
        context_builder=Context(),
        tool_registry=registry,
        approval_handler=lambda request: approvals.append(request),
        event_callback=events.append,
    )

    assert executed == [{"query": "real-smoke"}]
    assert approvals == []
    assert "SJTUCLAW_REAL_RECALL_RESULT" in result
    assert any(
        isinstance(event, ToolCallStartEvent)
        and event.tool_name.endswith("__recall")
        for event in events
    )


@pytest.mark.skipif(
    os.getenv("SJTUCLAW_RUN_CLAUDE_INTEGRATION") != "1",
    reason="set SJTUCLAW_RUN_CLAUDE_INTEGRATION=1 to launch installed Claude Code",
)
def test_real_claude_write_is_blocked_when_sjtuclaw_rejects(
    tmp_path,
    monkeypatch,
):
    command = resolve_claude_code_command()
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: ClaudeCodeRuntimeConfig(
            command=command,
            cwd=tmp_path,
            permission_mode="default",
            trust_tools=False,
            turn_timeout_s=180,
        ),
    )
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="claude-real-write")
    client = ClaudeCodeAgentClient(LLMConfig("", "", ""))
    approvals = []

    def reject(request):
        approvals.append(request)
        request.status = ApprovalStatus.REJECTED.value
        request.reject_reason = "real smoke rejection"
        return request

    client.run_agent_turn(
        "claude-real-write",
        (
            "Use Claude Code's Write tool, not Bash, to create danger.txt in "
            "the current directory with content BLOCKED. Attempt it exactly "
            "once, then report the outcome."
        ),
        session_store=store,
        approval_handler=reject,
    )

    assert [request.tool_name for request in approvals] == [
        "Claude Code / Write"
    ]
    assert not (tmp_path / "danger.txt").exists()


@pytest.mark.skipif(
    os.getenv("SJTUCLAW_RUN_CLAUDE_INTEGRATION") != "1",
    reason="set SJTUCLAW_RUN_CLAUDE_INTEGRATION=1 to launch installed Claude Code",
)
def test_real_claude_web_search_does_not_request_sjtuclaw_approval(
    tmp_path,
    monkeypatch,
):
    command = resolve_claude_code_command()
    monkeypatch.setattr(
        "claw.claude.client.load_claude_code_config",
        lambda: ClaudeCodeRuntimeConfig(
            command=command,
            cwd=tmp_path,
            permission_mode="default",
            trust_tools=False,
            turn_timeout_s=180,
        ),
    )
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="claude-real-search")
    client = ClaudeCodeAgentClient(LLMConfig("", "", ""))
    approvals = []
    events = []

    client.run_agent_turn(
        "claude-real-search",
        (
            "Use Claude Code's WebSearch tool exactly once to search for "
            '"OpenAI official website". Do not use another tool. Then report '
            "that the search completed."
        ),
        session_store=store,
        approval_handler=lambda request: approvals.append(request),
        event_callback=events.append,
    )

    assert approvals == []
    assert any(
        isinstance(event, ToolCallStartEvent)
        and event.tool_name == "WebSearch"
        for event in events
    )
