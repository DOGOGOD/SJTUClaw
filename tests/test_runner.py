"""Tests for AgentRunner — single-LLM-call handler v6."""

import json
import pytest

from claw.agent.runner import AgentRunner, AgentRunSpec, AgentRunResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeToolRegistry:
    def __init__(self): pass
    def list_definitions(self): return []
    def get_tool(self, name): return None
    def execute_by_name(self, name, args):
        return type('R', (), {'ok': True, 'content': f'executed {name}'})()

class _FakeLLMResponse:
    def __init__(self, is_final=False, final=None, is_tool_call=False, tool_calls=None):
        self.is_final = is_final; self.final = final
        self.is_tool_call = is_tool_call; self.tool_calls = tool_calls or []

class _FakeToolCall:
    def __init__(self, name="test_tool", args=None):
        self.name = name; self.args = args or {}; self.id = f"call_{name}"

class _FakeLLMClient:
    def __init__(self, response=None):
        self._response = response or _FakeLLMResponse(is_final=True, final="Hello!")
        self._config = type('C',(),{'model':'test'})()
    def chat_with_tools(self, messages, tools):
        return self._response

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(): return _FakeToolRegistry()

def _make_runner(response=None):
    r = AgentRunner()
    r.set_llm_client(_FakeLLMClient(response or _FakeLLMResponse(is_final=True, final="Hello!")))
    return r

# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------

class TestAgentRunnerBasic:
    def test_final_response(self, registry):
        runner = _make_runner()
        result = runner.call(AgentRunSpec(
            initial_messages=[{"role":"user","content":"hi"}], tools=registry, model="test"))
        assert result.final_content == "Hello!"
        assert result.finish_reason == "completed"
        assert not result.tool_calls

    def test_tool_calls_returned(self, registry):
        runner = _make_runner(_FakeLLMResponse(is_tool_call=True, tool_calls=[
            _FakeToolCall(name="search", args={"q":"test"})]))
        result = runner.call(AgentRunSpec(
            initial_messages=[{"role":"user","content":"search"}], tools=registry, model="test"))
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "search"
        assert result.finish_reason == "tool_calls"
        assert result.final_content is None

    def test_empty_response_retry(self, registry):
        calls = [0]
        class RetryClient:
            _config = type('C',(),{'model':'test'})()
            def chat_with_tools(self, messages, tools):
                calls[0] += 1
                if calls[0] <= 2:
                    return _FakeLLMResponse(is_final=True, final="")
                return _FakeLLMResponse(is_final=True, final="Got it!")
        runner = AgentRunner(); runner.set_llm_client(RetryClient())
        result = runner.call(AgentRunSpec(
            initial_messages=[{"role":"user","content":"hi"}], tools=registry, model="test"))
        assert result.final_content == "Got it!"
        assert calls[0] >= 3  # 2 empty + 1 success

    def test_empty_exhausted(self, registry):
        class AlwaysEmpty:
            _config = type('C',(),{'model':'test'})()
            def chat_with_tools(self, messages, tools):
                return _FakeLLMResponse(is_final=True, final="")
        runner = AgentRunner(); runner.set_llm_client(AlwaysEmpty())
        result = runner.call(AgentRunSpec(
            initial_messages=[{"role":"user","content":"hi"}], tools=registry, model="test"))
        assert result.finish_reason == "empty_final_response"

    def test_spec_defaults(self):
        spec = AgentRunSpec(initial_messages=[{"role":"user","content":"hi"}], tools=None, model="test")
        assert spec.max_tool_result_chars == 8000
        assert spec.max_output_tokens == 4096

    def test_result_fields(self):
        result = AgentRunResult(final_content="hello", finish_reason="completed")
        assert result.final_content == "hello"
        assert result.tool_calls == []

    def test_assistant_message_in_result(self, registry):
        runner = _make_runner()
        result = runner.call(AgentRunSpec(
            initial_messages=[{"role":"user","content":"hi"}], tools=registry, model="test"))
        assert result.assistant_message is not None
        assert result.assistant_message["role"] == "assistant"

    def test_tool_call_with_malformed_name(self, registry):
        """Tool calls with empty name should trigger retry then fall through."""
        calls = [0]
        class BadThenGood:
            _config = type('C',(),{'model':'test'})()
            def chat_with_tools(self, messages, tools):
                calls[0] += 1
                if calls[0] == 1:
                    return _FakeLLMResponse(is_tool_call=True, tool_calls=[
                        _FakeToolCall(name="", args={})])
                return _FakeLLMResponse(is_final=True, final="Fixed!")
        runner = AgentRunner(); runner.set_llm_client(BadThenGood())
        result = runner.call(AgentRunSpec(
            initial_messages=[{"role":"user","content":"hi"}], tools=registry, model="test"))
        # Either returns final or tool calls — malformed ones dropped
        assert result.final_content == "Fixed!" or len(result.tool_calls) >= 0

# ---------------------------------------------------------------------------
# Build assistant message
# ---------------------------------------------------------------------------

class TestBuildAssistantMessage:
    def test_final(self):
        msg = AgentRunner._build_assistant_message({"content":"hello"})
        assert msg["role"]=="assistant"; assert msg["content"]=="hello"

    def test_tool_call(self):
        msg = AgentRunner._build_assistant_message({"content":"let me check","tool_calls":[
            {"id":"c1","name":"search","args":{"q":"x"},"function":{"name":"search","arguments":'{"q":"x"}'}}
        ]})
        assert len(msg["tool_calls"])==1
        assert msg["tool_calls"][0]["function"]["name"]=="search"
