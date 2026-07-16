"""Regression tests: every tool-using turn must end with a user-visible reply."""

from __future__ import annotations

from claw.agent.events import (
    ErrorEvent,
    FinalEvent,
    ThinkingEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from claw.agent.loop import get_session_metrics_summary, run_agent_turn
from claw.approval.manager import ApprovalRequest
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


class _RecordingSequenceLLM(_SequenceLLM):
    def __init__(self, *items):
        super().__init__(*items)
        self.requests = []

    def chat_with_tools(self, messages, tool_defs):
        self.requests.append(messages)
        return super().chat_with_tools(messages, tool_defs)


class _ProtocolContext(_Context):
    def build_messages(self, session, **kwargs):
        return [message.to_dict() for message in session.messages]


class _SkillContext(_ProtocolContext):
    def build_skill_injection_message(self, skill_name, user_task):
        return f"[skill:{skill_name}]\n{user_task}"


class _SkillRegistry:
    def get_skill(self, name):
        return object() if name == "focused-skill" else None


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


def test_tool_result_replay_uses_native_protocol_fields(tmp_path):
    store = _store(tmp_path, "native-tool-protocol")
    client = _RecordingSequenceLLM(
        AgentResponse(
            tool_calls=[
                ToolCallRequest(name="probe", args={}, call_id="call-probe")
            ]
        ),
        AgentResponse(final="探测完成"),
    )

    reply = run_agent_turn(
        "native-tool-protocol",
        "请执行探测",
        session_store=store,
        context_builder=_ProtocolContext(),
        tool_registry=_registry(),
        llm_client=client,
    )

    assert reply == "探测完成"
    assert "任务处理简报" not in reply
    second_request = client.requests[1]
    assistant_call = next(
        message
        for message in second_request
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    tool_result = next(
        message for message in second_request if message["role"] == "tool"
    )
    assert assistant_call["tool_calls"][0]["id"] == "call-probe"
    assert assistant_call["tool_calls"][0]["function"]["name"] == "probe"
    assert tool_result["tool_call_id"] == "call-probe"
    assert tool_result["name"] == "probe"

    saved = store.get("native-tool-protocol")
    assert saved.messages[-3].tool_calls
    assert saved.messages[-2].tool_call_id == "call-probe"


def test_batched_skill_injection_follows_all_tool_results(tmp_path):
    store = _store(tmp_path, "skill-tool-protocol")
    registry = _registry()
    registry.register(
        Tool(
            name="use_skill",
            description="load a skill",
            input_schema={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["skill_name"],
            },
            handler=lambda args: ToolResult(ok=True, content="不应直接执行"),
            safety_level="skill_select",
        )
    )
    client = _RecordingSequenceLLM(
        AgentResponse(
            tool_calls=[
                ToolCallRequest(
                    name="use_skill",
                    args={"skill_name": "focused-skill", "reason": "匹配任务"},
                    call_id="call-skill",
                ),
                ToolCallRequest(name="probe", args={}, call_id="call-probe"),
            ]
        ),
        AgentResponse(final="任务完成"),
    )

    reply = run_agent_turn(
        "skill-tool-protocol",
        "请执行组合任务",
        session_store=store,
        context_builder=_SkillContext(),
        tool_registry=registry,
        llm_client=client,
        skill_registry=_SkillRegistry(),
    )

    assert reply == "任务完成"
    assert "任务处理简报" not in reply
    second_request = client.requests[1]
    call_index = next(
        index
        for index, message in enumerate(second_request)
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert [message["role"] for message in second_request[call_index + 1 :]] == [
        "tool",
        "tool",
        "user",
    ]
    assert second_request[call_index + 1]["tool_call_id"] == "call-skill"
    assert second_request[call_index + 2]["tool_call_id"] == "call-probe"
    assert second_request[call_index + 3]["content"].startswith("[skill:focused-skill]")


def test_duplicate_tool_call_ids_are_normalized(tmp_path):
    store = _store(tmp_path, "duplicate-call-ids")
    session = store.get("duplicate-call-ids")
    session.append_message(
        "assistant",
        "",
        tool_calls=[
            {
                "id": "duplicate",
                "type": "function",
                "function": {"name": "probe", "arguments": "{}"},
            }
        ],
    )
    session.append_message(
        "tool",
        "历史结果",
        tool_call_id="duplicate",
        name="probe",
    )
    store.save(session)
    client = _RecordingSequenceLLM(
        AgentResponse(
            tool_calls=[
                ToolCallRequest(name="probe", args={}, call_id="duplicate"),
                ToolCallRequest(name="probe", args={}, call_id="duplicate"),
            ]
        ),
        AgentResponse(final="完成"),
    )

    reply = run_agent_turn(
        "duplicate-call-ids",
        "执行两次探测",
        session_store=store,
        context_builder=_ProtocolContext(),
        tool_registry=_registry(),
        llm_client=client,
    )

    assert reply == "完成"
    second_request = client.requests[1]
    assistant_indexes = [
        index
        for index, message in enumerate(second_request)
        if message.get("tool_calls")
    ]
    assistant_call = second_request[assistant_indexes[-1]]
    declared_ids = [call["id"] for call in assistant_call["tool_calls"]]
    result_ids = [
        message["tool_call_id"]
        for message in second_request[assistant_indexes[-1] + 1 :]
        if message["role"] == "tool"
    ]
    assert len(set(declared_ids)) == 2
    assert result_ids == declared_ids


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
    assert "任务处理简报" in reply
    assert "探测完成" in reply
    assert "没有生成最终回复" in reply
    assert store.get("empty-after-tool").messages[-1].content == reply


def test_truncated_final_includes_failure_brief(tmp_path):
    store = _store(tmp_path, "truncated-final")

    reply = run_agent_turn(
        "truncated-final",
        "生成较长回答",
        session_store=store,
        context_builder=_Context(),
        tool_registry=_registry(),
        llm_client=_SequenceLLM(
            AgentResponse(final="这是未完整的回答", finish_reason="length")
        ),
    )

    assert reply.startswith("这是未完整的回答")
    assert "任务处理简报" in reply
    assert "状态：部分完成" in reply
    assert "输出长度限制" in reply


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
    assert "任务处理简报" in reply
    assert "状态：未完成" in reply
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


def test_pending_approval_never_executes_mutating_tool(tmp_path):
    store = _store(tmp_path, "pending-approval")
    executed = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="write_probe",
            description="mutating test tool",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: executed.append(True) or ToolResult(ok=True, content="已写入"),
            safety_level="write",
        )
    )

    reply = run_agent_turn(
        "pending-approval",
        "执行写入",
        session_store=store,
        context_builder=_Context(),
        tool_registry=registry,
        llm_client=_SequenceLLM(
            AgentResponse(tool_calls=[ToolCallRequest(name="write_probe", args={})]),
            AgentResponse(final="写入未获批准"),
        ),
        approval_handler=lambda req: ApprovalRequest(
            session_id=req.session_id,
            tool_name=req.tool_name,
            tool_args=req.tool_args,
        ),
    )

    assert reply == "写入未获批准"
    assert executed == []
    assert "审批未明确通过" in store.get("pending-approval").messages[-2].content


def test_approval_callback_exception_becomes_safe_observation(tmp_path):
    store = _store(tmp_path, "approval-exception")
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="write_probe",
            description="mutating test tool",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: ToolResult(ok=True, content="不应执行"),
            safety_level="write",
        )
    )

    def broken_approval(req):
        raise RuntimeError("approval transport disconnected")

    reply = run_agent_turn(
        "approval-exception",
        "执行写入",
        session_store=store,
        context_builder=_Context(),
        tool_registry=registry,
        llm_client=_SequenceLLM(
            AgentResponse(tool_calls=[ToolCallRequest(name="write_probe", args={})]),
            AgentResponse(final="操作已安全停止"),
        ),
        approval_handler=broken_approval,
    )

    assert reply == "操作已安全停止"
    assert "transport disconnected" in store.get("approval-exception").messages[-2].content


def test_repeated_identical_tool_call_stops_with_completion_brief(tmp_path):
    store = _store(tmp_path, "repeat-tool")
    calls = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="probe",
            description="test tool",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: calls.append(True) or ToolResult(ok=True, content="探测完成"),
        )
    )
    repeated = AgentResponse(tool_calls=[ToolCallRequest(name="probe", args={})])

    reply = run_agent_turn(
        "repeat-tool",
        "不要无限循环",
        session_store=store,
        context_builder=_Context(),
        tool_registry=registry,
        llm_client=_SequenceLLM(repeated, repeated, repeated, repeated, repeated),
    )

    assert len(calls) == 3
    assert "任务处理简报" in reply
    assert "重复" in reply or "工具调用已关闭" in reply
    assert store.get("repeat-tool").messages[-1].content == reply


def test_turn_metrics_are_aggregated(tmp_path):
    store = _store(tmp_path, "metrics")
    run_agent_turn(
        "metrics",
        "正常完成",
        session_store=store,
        context_builder=_Context(),
        tool_registry=_registry(),
        llm_client=_SequenceLLM(AgentResponse(final="完成")),
    )

    summary = get_session_metrics_summary("metrics")
    assert summary is not None
    assert summary["turns"] == 1
    assert summary["llm_calls"] == 1
    assert summary["health"]["turns_monitored"] == 1
