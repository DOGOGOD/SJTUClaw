"""Tests for agent turn cancellation via cancel_event.

Covers:
- run_agent_turn with cancel_event set before the loop starts
- cancel_event set mid-loop (between iterations)
- /stop endpoint behaviour (via _cancel_active_turn helpers)
- /stop slash command
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from claw.agent.loop import run_agent_turn
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.session.store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeLLMResponse:
    """Minimal fake LLM response."""

    def __init__(self, final: str = "", is_tool_call: bool = False, tool_calls=None):
        self.final = final
        self.is_final = bool(final) and not is_tool_call
        self.is_tool_call = is_tool_call
        self.tool_calls = tool_calls or []
        self.finish_reason = "stop"


class _FakeLLMClient:
    """LLM client that returns canned responses, optionally with delays."""

    def __init__(self, responses: list, delay: float = 0.0):
        self._responses = list(responses)
        self._delay = delay
        self.call_count = 0
        self.config = MagicMock()
        self.config.context_window = 8000
        self.config.max_output_tokens = 2000

    def chat_with_tools(self, messages, tool_defs, **kwargs):
        self.call_count += 1
        if self._delay > 0:
            time.sleep(self._delay)
        if not self._responses:
            return _FakeLLMResponse(final="完成")
        return self._responses.pop(0)

    def chat(self, messages, **kwargs):
        self.call_count += 1
        return "测试标题"


class _FakeToolRegistry:
    """Minimal tool registry."""

    def get_tool(self, name):
        return None

    def get_tool_definitions(self):
        return []


def _make_context_builder(system_prompt="test"):
    cb = MagicMock(spec=ContextBuilder)
    cb.build_messages.return_value = [{"role": "user", "content": "test"}]
    cb.get_tool_definitions.return_value = []
    return cb


# ---------------------------------------------------------------------------
# run_agent_turn cancellation tests
# ---------------------------------------------------------------------------


class TestRunAgentTurnCancellation:
    """Tests that run_agent_turn respects the cancel_event parameter."""

    @pytest.fixture
    def store(self, tmp_path):
        return SessionStore(tmp_path / "sessions")

    @pytest.fixture
    def context_builder(self):
        return _make_context_builder()

    @pytest.fixture
    def tool_registry(self):
        return _FakeToolRegistry()

    def test_cancel_before_loop_starts(self, store, context_builder, tool_registry):
        """When cancel_event is already set, the turn should exit immediately."""
        session = store.create_session(session_id="cancel1")
        store.save(session)

        cancel_event = threading.Event()
        cancel_event.set()  # Pre-set

        client = _FakeLLMClient([_FakeLLMResponse(final="不应该到达这里")])

        result = run_agent_turn(
            "cancel1",
            "test message",
            session_store=store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
            cancel_event=cancel_event,
        )

        assert "终止" in result
        # LLM should never have been called
        assert client.call_count == 0

    def test_cancel_mid_loop(self, store, context_builder, tool_registry):
        """When cancel_event is set mid-loop, the turn should exit on next iteration."""
        session = store.create_session(session_id="cancel2")
        store.save(session)

        cancel_event = threading.Event()

        # First call returns a tool call (with delay so cancel has time to fire),
        # second should never happen
        client = _FakeLLMClient(
            [_FakeLLMResponse(is_tool_call=True, tool_calls=[])],
            delay=0.2,
        )

        # Set cancel event after a short delay
        def _set_cancel():
            time.sleep(0.1)
            cancel_event.set()

        threading.Thread(target=_set_cancel, daemon=True).start()

        result = run_agent_turn(
            "cancel2",
            "test message",
            session_store=store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
            cancel_event=cancel_event,
        )

        assert "终止" in result

    def test_no_cancel_event_works_normally(self, store, context_builder, tool_registry):
        """Without cancel_event, the turn should complete normally."""
        session = store.create_session(session_id="normal")
        store.save(session)

        client = _FakeLLMClient([_FakeLLMResponse(final="正常回复")])

        result = run_agent_turn(
            "normal",
            "test message",
            session_store=store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
        )

        assert result == "正常回复"

    def test_cancelled_session_has_terminated_message(self, store, context_builder, tool_registry):
        """The session should contain the cancellation message after cancel."""
        session = store.create_session(session_id="cancel3")
        store.save(session)

        cancel_event = threading.Event()
        cancel_event.set()

        client = _FakeLLMClient([])

        run_agent_turn(
            "cancel3",
            "test message",
            session_store=store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
            cancel_event=cancel_event,
        )

        session = store.get("cancel3")
        # Should have user message + assistant cancel message
        assert any(m.role == "assistant" and "终止" in m.content for m in session.messages)


# ---------------------------------------------------------------------------
# /stop endpoint helper tests
# ---------------------------------------------------------------------------


class TestStopHelpers:
    """Tests for _register_active_turn / _cancel_active_turn helpers."""

    def test_register_and_cancel(self):
        from claw.gateway.server import (
            _register_active_turn,
            _cancel_active_turn,
            _unregister_active_turn,
        )

        event = _register_active_turn("test_session_1")
        assert not event.is_set()

        found = _cancel_active_turn("test_session_1")
        assert found is True
        assert event.is_set()

        # Cleanup
        _unregister_active_turn("test_session_1")

    def test_cancel_nonexistent_returns_false(self):
        from claw.gateway.server import _cancel_active_turn

        found = _cancel_active_turn("nonexistent_session")
        assert found is False

    def test_cancel_all(self):
        from claw.gateway.server import (
            _register_active_turn,
            _cancel_all_active_turns,
            _unregister_active_turn,
        )

        e1 = _register_active_turn("test_all_1")
        e2 = _register_active_turn("test_all_2")

        count = _cancel_all_active_turns()
        assert count >= 2
        assert e1.is_set()
        assert e2.is_set()

        # Cleanup
        _unregister_active_turn("test_all_1")
        _unregister_active_turn("test_all_2")

    def test_unregister_removes_from_tracking(self):
        from claw.gateway.server import (
            _register_active_turn,
            _cancel_active_turn,
            _unregister_active_turn,
        )

        _register_active_turn("test_unregister")
        _unregister_active_turn("test_unregister")

        found = _cancel_active_turn("test_unregister")
        assert found is False


# ---------------------------------------------------------------------------
# /stop slash command tests
# ---------------------------------------------------------------------------


class TestStopSlashCommand:
    """Tests for the /stop slash command."""

    def test_stop_command_with_no_active_turn(self):
        from claw.gateway.server import _execute_slash_command

        result = _execute_slash_command("/stop", "test_stop_session")
        assert "当前没有正在运行" in result or "终止" in result

    def test_stop_command_with_active_turn(self):
        from claw.gateway.server import (
            _register_active_turn,
            _execute_slash_command,
            _unregister_active_turn,
        )

        event = _register_active_turn("test_stop_active")
        result = _execute_slash_command("/stop", "test_stop_active")

        assert "终止" in result
        assert event.is_set()

        _unregister_active_turn("test_stop_active")

    def test_stop_in_command_prefixes(self):
        from claw.cli.commands import _COMMAND_PREFIXES

        assert "/stop" in _COMMAND_PREFIXES

    def test_stop_is_slash_command(self):
        from claw.gateway.server import _is_slash_command

        assert _is_slash_command("/stop")
        assert _is_slash_command("/stop extra args")
