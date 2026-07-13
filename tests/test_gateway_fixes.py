"""Tests for gateway optimisation and full-code-review bug fixes.



Covers:

- LLM client: secret scrubbing, defensive tool_call parsing.

- Workspace manager: thread-safe concurrent access.

- Session store: session_id validation (path traversal, control chars).

- Context governance: empty-message-list safety net.

- Tools: prepare_call exception logging, error-result truncation.

- Gateway middleware: rate limiter, request size, error sanitisation.

"""

from __future__ import annotations



import json as json_module

import shutil

import tempfile

import threading

from pathlib import Path



import pytest





# -- fixtures ----------------------------------------------------------





@pytest.fixture

def tmp():

    d = Path(tempfile.mkdtemp())

    yield d

    shutil.rmtree(str(d), ignore_errors=True)





@pytest.fixture

def ss(tmp):

    from claw.session.store import SessionStore

    return SessionStore(tmp / "sessions")





@pytest.fixture

def wm():

    from claw.workspace.manager import WorkspaceManager

    return WorkspaceManager()





@pytest.fixture

def reg():

    from claw.tools.base import ToolRegistry, Tool, ToolResult

    r = ToolRegistry()



    def _ok_handler(args):

        return ToolResult(ok=True, content="ok")



    def _err_handler(args):

        return ToolResult(ok=False, error="boom")



    r.register(Tool(

        name="ok_tool",

        description="always succeeds",

        input_schema={"type": "object", "properties": {}},

        handler=_ok_handler,

        max_result_chars=10,

    ))

    r.register(Tool(

        name="err_tool",

        description="always fails",

        input_schema={"type": "object", "properties": {}},

        handler=_err_handler,

        max_result_chars=10,

    ))

    return r





# -- LLM client: secret scrubbing --------------------------------------





class TestSecretScrubbing:

    def test_scrubs_openai_style_key(self):

        from claw.llm.client import _scrub_secrets

        msg = "Auth failed for sk-abcd1234efgh5678ijkl9012mnop3456"

        result = _scrub_secrets(msg)

        assert "sk-abcd1234" not in result

        assert "REDACTED" in result



    def test_scrubs_bearer_token(self):

        from claw.llm.client import _scrub_secrets

        msg = "Bearer sk-supersecrettoken1234567890abc"

        result = _scrub_secrets(msg)

        assert "supersecrettoken" not in result



    def test_scrubs_api_key_assignment(self):

        from claw.llm.client import _scrub_secrets

        msg = 'api_key="abcdefghijklmnop1234567890"'

        result = _scrub_secrets(msg)

        assert "abcdefghijklmnop" not in result



    def test_preserves_non_secret_text(self):

        from claw.llm.client import _scrub_secrets

        msg = "Connection refused to http://localhost:8000"

        assert _scrub_secrets(msg) == msg



    def test_handles_empty(self):

        from claw.llm.client import _scrub_secrets

        assert _scrub_secrets("") == ""

        assert _scrub_secrets(None) is None





# -- LLM client: defensive tool_call parsing ---------------------------





class TestToolCallParsing:

    def test_skips_tool_call_without_function(self):

        """A tool_call with function=None should be skipped, not crash."""

        from claw.llm.client import LLMClient

        from claw.config import LLMConfig



        # Build a fake response object mimicking openai's structure

        class FakeFunction:

            def __init__(self, name, arguments):

                self.name = name

                self.arguments = arguments



        class FakeToolCall:

            def __init__(self, id, function):

                self.id = id

                self.function = function



        class FakeMessage:

            def __init__(self, tool_calls, content=None):

                self.tool_calls = tool_calls

                self.content = content



        class FakeChoice:

            def __init__(self, message, finish_reason="stop"):

                self.message = message

                self.finish_reason = finish_reason



        class FakeResponse:

            def __init__(self, choices):

                self.choices = choices



        # One good call + one malformed (function=None)

        good_tc = FakeToolCall("call_1", FakeFunction("read", '{"path":"x"}'))

        bad_tc = FakeToolCall("call_2", None)

        msg = FakeMessage([good_tc, bad_tc], content=None)

        resp = FakeResponse([FakeChoice(msg)])



        # Monkey-patch the client to return our fake response

        config = LLMConfig(

            api_key="sk-test",

            base_url="http://localhost",

            model="test",

            context_window=4096,

        )

        client = LLMClient(config)

        client._call_api = lambda *a, **kw: resp



        result = client.chat_with_tools([], tool_definitions=[])

        assert result.is_tool_call

        assert len(result.tool_calls) == 1

        assert result.tool_calls[0].name == "read"





# -- Workspace manager: thread safety ----------------------------------





class TestWorkspaceThreadSafety:

    def test_concurrent_set_get_unset(self, wm, tmp):

        """Concurrent set/get/unset should not raise or corrupt state."""

        results: list = []

        errors: list = []

        # Create the workspace directories so wm.set() doesn't fail
        ws_paths = []
        for i in range(8):
            p = tmp / f"ws{i}"
            p.mkdir(parents=True, exist_ok=True)
            ws_paths.append(p)


        def worker(session_id: str, path: Path):

            try:

                for _ in range(100):

                    wm.set(session_id, str(path))

                    wm.get(session_id)

                    wm.require(session_id)

                    wm.unset(session_id)

                    wm.get(session_id)

            except Exception as e:

                errors.append(e)



        threads = [

            threading.Thread(target=worker, args=(f"s{i}", ws_paths[i]))

            for i in range(8)

        ]

        for t in threads:

            t.start()

        for t in threads:

            t.join()



        assert not errors, f"Concurrent access raised: {errors}"



    def test_require_raises_after_unset(self, wm, tmp):

        wm.set("s1", str(tmp))

        wm.unset("s1")

        from claw.workspace.manager import WorkspaceError

        with pytest.raises(WorkspaceError):

            wm.require("s1")





# -- Session store: session_id validation ------------------------------





class TestSessionIdValidation:

    def test_rejects_empty_id(self, ss):

        from claw.session.store import SessionStoreError

        # create_session("") generates a new id (falsy short-circuit),

        # but get("") should reject it.

        with pytest.raises(SessionStoreError):

            ss.get("")



    def test_rejects_path_separator(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.create_session(session_id="evil/../path")



    def test_rejects_backslash(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.create_session(session_id="evil\\path")



    def test_rejects_null_byte(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.create_session(session_id="evil\x00root")



    def test_rejects_newline(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.create_session(session_id="evil\nroot")



    def test_rejects_control_chars(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.create_session(session_id="bad\x01id")



    def test_rejects_overlong_id(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.create_session(session_id="x" * 300)



    def test_accepts_valid_id(self, ss):

        s = ss.create_session(session_id="valid-session-123")

        assert s.session_id == "valid-session-123"



    def test_exists_returns_false_for_invalid_id(self, ss):

        assert ss.exists("../bad") is False



    def test_get_raises_for_invalid_id(self, ss):

        from claw.session.store import SessionStoreError

        with pytest.raises(SessionStoreError):

            ss.get("bad\x00id")





# -- Context governance: empty message safety net ----------------------





class TestContextGovernanceSafetyNet:

    def test_snip_keeps_at_least_one_message(self):

        """Even when all messages exceed budget, at least one is kept."""

        from claw.context.governance import ContextGovernor, GovernanceConfig



        # Create a single huge user message that exceeds the budget

        huge_content = "x" * 100_000

        messages = [

            {"role": "system", "content": "sys"},

            {"role": "user", "content": huge_content},

        ]

        config = GovernanceConfig(

            max_tool_result_chars=8000,

            context_window_tokens=100,  # tiny budget

            max_output_tokens=50,

        )

        gov = ContextGovernor()

        result = gov._snip_history(config, messages)

        # Must keep system + at least one non-system message

        assert any(m.get("role") == "system" for m in result)

        assert any(m.get("role") != "system" for m in result)





# -- Tools: prepare_call logging + error truncation --------------------





class TestToolRegistryFixes:

    def test_prepare_call_exception_does_not_crash(self, reg):

        """A failed guard hook must block execution without crashing."""

        from claw.tools.base import ToolResult



        def bad_hook(name, args):

            raise RuntimeError("hook crashed")



        reg.set_prepare_call(bad_hook)

        result = reg.execute_by_name("ok_tool", {})

        assert not result.ok

        assert "安全中止" in result.error



    def test_error_result_truncated(self, reg):

        """Error results should also be truncated to max_result_chars."""

        from claw.tools.base import Tool, ToolResult, ToolRegistry



        long_error = "E" * 500

        r = ToolRegistry()

        r.register(Tool(

            name="verbose_failer",

            description="fails with a long message",

            input_schema={"type": "object", "properties": {}},

            handler=lambda args: ToolResult(ok=False, error=long_error),

            max_result_chars=20,

        ))

        result = r.execute_by_name("verbose_failer", {})

        assert not result.ok

        # 20 chars + "\n...[truncated]" marker (14 chars) = 34

        assert len(result.error) <= 40

        assert "truncated" in result.error



    def test_success_result_still_truncated(self, reg):

        """Success results continue to be truncated."""

        from claw.tools.base import Tool, ToolResult, ToolRegistry



        long_content = "A" * 500

        r = ToolRegistry()

        r.register(Tool(

            name="verbose_succeeder",

            description="succeeds with a long message",

            input_schema={"type": "object", "properties": {}},

            handler=lambda args: ToolResult(ok=True, content=long_content),

            max_result_chars=20,

        ))

        result = r.execute_by_name("verbose_succeeder", {})

        assert result.ok

        assert len(result.content) <= 40

        assert "truncated" in result.content





# -- Gateway middleware: rate limiter ----------------------------------





class TestRateLimiter:

    def test_allows_burst_then_blocks(self):

        from claw.gateway.middleware import RateLimiter

        rl = RateLimiter(max_requests=5, window_s=60.0, burst=2)

        # First 2 (burst) should pass

        ok1, rem1 = rl.check("client_a")

        ok2, rem2 = rl.check("client_a")

        assert ok1 and ok2

        # Subsequent within window should still pass up to max_requests

        for _ in range(3):

            rl.check("client_a")

        # 6th request should be blocked

        ok6, rem6 = rl.check("client_a")

        assert not ok6



    def test_separate_clients(self):

        from claw.gateway.middleware import RateLimiter

        rl = RateLimiter(max_requests=3, window_s=60.0, burst=3)

        for _ in range(3):

            rl.check("a")

        ok_a, _ = rl.check("a")

        ok_b, _ = rl.check("b")

        assert not ok_a

        assert ok_b

    def test_only_marked_loopback_pet_runtime_requests_are_internal(self):
        from starlette.requests import Request
        from claw.gateway.middleware import _is_internal_pet_request

        def request(path, host, marked):
            headers = [(b"x-sjtuclaw-internal", b"desktop-pet")] if marked else []
            return Request({
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": headers,
                "client": (host, 12345),
                "server": ("testserver", 80),
                "scheme": "http",
                "query_string": b"",
            })

        assert _is_internal_pet_request(
            request("/pet/state", "127.0.0.1", True), "/pet/state"
        )
        assert not _is_internal_pet_request(
            request("/pet/state", "10.0.0.2", True), "/pet/state"
        )
        assert not _is_internal_pet_request(
            request("/pet/settings", "127.0.0.1", True), "/pet/settings"
        )
        assert not _is_internal_pet_request(
            request("/pet/state", "127.0.0.1", False), "/pet/state"
        )





class TestSanitizeErrorMessage:

    def test_strips_file_paths(self):

        from claw.gateway.middleware import sanitize_error_message

        exc = FileNotFoundError("/etc/passwd/secret.key")

        msg = sanitize_error_message(exc)

        assert "/etc/passwd" not in msg or "secret" not in msg



    def test_returns_generic_for_unknown(self):

        from claw.gateway.middleware import sanitize_error_message

        exc = RuntimeError("some weird error")

        msg = sanitize_error_message(exc)

        assert isinstance(msg, str)

        assert len(msg) > 0



    def test_includes_detail_when_requested(self):

        from claw.gateway.middleware import sanitize_error_message

        exc = ValueError("specific detail")

        msg = sanitize_error_message(exc, include_detail=True)

        assert isinstance(msg, str)





# ── Protocol: tool call truncation ────────────────────────────────────





class TestToolCallTruncation:

    """Verify that exceeding MAX_TOOL_CALLS_PER_TURN truncates instead

    of raising ProtocolParseError.



    This was the root cause of the "根据项目结构添加 README 文件" task

    failure: the LLM reasonably requested 11-19 parallel read-only calls

    to explore the project, but the old hard-coded limit of 5 caused a

    hard error that the agent loop couldn't recover from.

    """



    def test_native_tool_calls_truncated_not_error(self):

        """Native tool_calls exceeding the limit should be truncated."""

        from claw.llm.protocol import parse_agent_response, MAX_TOOL_CALLS_PER_TURN



        # Build more calls than the limit

        excess = MAX_TOOL_CALLS_PER_TURN + 5

        native_calls = [

            {

                "id": f"call_{i}",

                "function": {

                    "name": "read",

                    "arguments": json_module.dumps({"path": f"file_{i}.txt"}),

                },

            }

            for i in range(excess)

        ]

        result = parse_agent_response(None, native_calls)

        assert result.is_tool_call

        assert len(result.tool_calls) == MAX_TOOL_CALLS_PER_TURN



    def test_json_tool_calls_truncated_not_error(self):

        """JSON-protocol tool_calls exceeding the limit should be truncated."""

        from claw.llm.protocol import parse_agent_response, MAX_TOOL_CALLS_PER_TURN



        excess = MAX_TOOL_CALLS_PER_TURN + 3

        calls_list = [

            {"tool": "list_dir", "args": {"path": f"dir_{i}"}}

            for i in range(excess)

        ]

        text = json_module.dumps({"type": "tool_calls", "calls": calls_list})

        result = parse_agent_response(text)

        assert result.is_tool_call

        assert len(result.tool_calls) == MAX_TOOL_CALLS_PER_TURN



    def test_within_limit_not_truncated(self):

        """Calls within the limit should pass through unchanged."""

        from claw.llm.protocol import parse_agent_response, MAX_TOOL_CALLS_PER_TURN



        count = min(MAX_TOOL_CALLS_PER_TURN, 3)

        native_calls = [

            {

                "id": f"call_{i}",

                "function": {"name": "read", "arguments": "{}"},

            }

            for i in range(count)

        ]

        result = parse_agent_response(None, native_calls)

        assert len(result.tool_calls) == count



    def test_limit_is_configurable(self):

        """MAX_TOOL_CALLS_PER_TURN should be >= 20 by default."""

        from claw.llm.protocol import MAX_TOOL_CALLS_PER_TURN

        assert MAX_TOOL_CALLS_PER_TURN >= 20



    def test_protocol_instructions_mention_limit(self):

        """The protocol instructions should tell the LLM the limit."""

        from claw.llm.protocol import build_protocol_instructions, MAX_TOOL_CALLS_PER_TURN

        text = build_protocol_instructions([])

        assert str(MAX_TOOL_CALLS_PER_TURN) in text



