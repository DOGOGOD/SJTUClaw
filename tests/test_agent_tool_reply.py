"""Regression tests: every tool-using turn must end with a user-visible reply."""

from __future__ import annotations

from claw.agent.events import (
    ErrorEvent,
    FinalEvent,
    ThinkingEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from claw.agent.loop import run_agent_turn
from claw.llm.protocol import AgentResponse, ToolCallRequest
from claw.session.store import SessionStore
from claw.tools.base import Tool, ToolRegistry, ToolResult


class _Context:
    def build_messages(self, session, **kwargs):
        return [{"role": message.role, "content": message.content} for message in session.messages]

    def get_tool_definitions(self, registry):
        return registry.list_definitions()


class _SequenceLLM:
    def __init__(self, *items):
        self.items = list(items)

    def chat_with_tools(self, messages, tool_defs):
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="probe",
            description="test tool",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: ToolResult(ok=True, content="探测完成"),
            concurrency_safe=True,
        )
    )
    return registry


def _store(tmp_path, session_id="reply-test"):
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id=session_id)
    store.save(session)
    return store


def test_llm_failure_after_tool_produces_persisted_final_reply(tmp_path):
    store = _store(tmp_path)
    events = []
    client = _SequenceLLM(
        AgentResponse(tool_calls=[ToolCallRequest(name="probe", args={}, call_id="call-1")]),
        RuntimeError("upstream disconnected"),
    )

    reply = run_agent_turn(
        "reply-test",
        "请执行探测",
        session_store=store,
        context_builder=_Context(),
        tool_registry=_registry(),
        llm_client=client,
        event_callback=events.append,
    )

    assert reply.strip()
    assert "工具调用已执行" in reply
    saved = store.get("reply-test")
    assert saved.messages[-1].role == "assistant"
    assert saved.messages[-1].content == reply
    assert any(isinstance(event, ThinkingEvent) for event in events)
    assert any(isinstance(event, ToolCallStartEvent) for event in events)
    assert any(isinstance(event, ToolCallEndEvent) for event in events)
    assert any(isinstance(event, ErrorEvent) for event in events)
    finals = [event for event in events if isinstance(event, FinalEvent)]
    assert len(finals) == 1
    assert finals[0].content == reply


def test_empty_final_after_tool_is_replaced_with_visible_reply(tmp_path):
    store = _store(tmp_path, "empty-after-tool")
    client = _SequenceLLM(
        AgentResponse(tool_calls=[ToolCallRequest(name="probe", args={})]),
        AgentResponse(final=""),
    )

    reply = run_agent_turn(
        "empty-after-tool",
        "执行后总结",
        session_store=store,
        context_builder=_Context(),
        tool_registry=_registry(),
        llm_client=client,
    )

    assert reply.strip()
    assert "没有生成最终回复" in reply
    assert store.get("empty-after-tool").messages[-1].content == reply


def test_unrecognized_response_gets_non_empty_fallback(tmp_path):
    store = _store(tmp_path, "invalid-response")
    events = []

    reply = run_agent_turn(
        "invalid-response",
        "测试空响应",
        session_store=store,
        context_builder=_Context(),
        tool_registry=_registry(),
        llm_client=_SequenceLLM(AgentResponse()),
        event_callback=events.append,
    )

    assert reply.strip()
    assert store.get("invalid-response").messages[-1].role == "assistant"
    assert isinstance(events[-1], FinalEvent)


def test_broken_event_callback_does_not_interrupt_reply(tmp_path):
    store = _store(tmp_path, "callback-error")

    def broken_callback(event):
        raise RuntimeError("UI disconnected")

    reply = run_agent_turn(
        "callback-error",
        "正常回复",
        session_store=store,
        context_builder=_Context(),
        tool_registry=_registry(),
        llm_client=_SequenceLLM(AgentResponse(final="完成")),
        event_callback=broken_callback,
    )

    assert reply == "完成"
    assert store.get("callback-error").messages[-1].content == "完成"
