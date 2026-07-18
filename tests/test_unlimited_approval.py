"""Tests for unlimited mode security: approval gate for dangerous operations.

Covers:
- _is_outside_workspace() helper (path + shell command detection)
- Approval gate enforcement in run_agent_turn:
  * unlimited + auto_mode + absolute path -> approval required
  * unlimited + auto_mode + ".." path -> approval required
  * unlimited + auto_mode + shell tool -> approval required (forced)
  * unlimited + auto_mode + relative path -> approval required
  * non-unlimited + auto_mode + relative path -> auto-approved
- /unlimited command registration and behavior
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from claw.agent.loop import _is_outside_workspace, run_agent_turn
from claw.context.builder import ContextBuilder
from claw.session.store import SessionStore


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal fake tool with a configurable safety_level."""

    def __init__(self, name: str, safety_level: str = "read"):
        self.name = name
        self.safety_level = safety_level
        self.concurrency_safe = False


class _FakeLLMResponse:
    """Minimal fake LLM response."""

    def __init__(self, final: str = "", is_tool_call: bool = False, tool_calls=None):
        self.final = final
        self.is_final = bool(final) and not is_tool_call
        self.is_tool_call = is_tool_call
        self.tool_calls = tool_calls or []
        self.finish_reason = "stop"


class _FakeLLMClient:
    """LLM client that returns canned responses."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.call_count = 0
        self.config = MagicMock()
        self.config.context_window = 8000
        self.config.max_output_tokens = 2000

    def chat_with_tools(self, messages, tool_defs, **kwargs):
        self.call_count += 1
        if not self._responses:
            return _FakeLLMResponse(final="done")
        return self._responses.pop(0)

    def chat(self, messages, **kwargs):
        return "ok"


class _FakeToolResult:
    def __init__(self, ok=True, content="ok", error=""):
        self.ok = ok
        self.content = content
        self.error = error


class _FakeToolRegistry:
    """Tool registry with configurable tools."""

    def __init__(self, tools: dict[str, _FakeTool] | None = None):
        self._tools = tools or {}

    def get_tool(self, name):
        return self._tools.get(name)

    def get_tool_definitions(self):
        return []

    def execute_by_name(self, name, args):
        return _FakeToolResult(ok=True, content="executed")


class _ApprovalCallTracker:
    """Approval handler that tracks calls and auto-decides."""

    def __init__(self, decision: str = "approved"):
        self.calls = []
        self._decision = decision

    def __call__(self, req):
        self.calls.append(req)
        from claw.approval.manager import ApprovalStatus
        req.status = (
            ApprovalStatus.APPROVED.value
            if self._decision == "approved"
            else ApprovalStatus.REJECTED.value
        )
        req.reject_reason = "" if self._decision == "approved" else "test rejection"
        return req


def _make_context_builder(system_prompt="test"):
    cb = MagicMock(spec=ContextBuilder)
    cb.build_messages.return_value = [{"role": "user", "content": "test"}]
    cb.get_tool_definitions.return_value = []
    return cb


# ---------------------------------------------------------------------------
# _is_outside_workspace unit tests
# ---------------------------------------------------------------------------


class TestIsOutsideWorkspace:
    """Tests for the _is_outside_workspace helper function."""

    def test_absolute_unix_path(self):
        assert _is_outside_workspace("write_file", {"file_path": "/etc/passwd"}) is True

    def test_absolute_windows_path(self):
        assert _is_outside_workspace("write_file", {"file_path": "C:\\Windows\\system32"}) is True

    def test_parent_traversal_path(self):
        assert _is_outside_workspace("write_file", {"file_path": "../../secret"}) is True

    def test_relative_path_is_safe(self):
        assert _is_outside_workspace("write_file", {"file_path": "src/main.py"}) is False

    def test_no_path_argument(self):
        assert _is_outside_workspace("read_file", {}) is False

    def test_non_string_path(self):
        assert _is_outside_workspace("write_file", {"file_path": 123}) is False

    def test_shell_command_with_absolute_unix_path(self):
        assert _is_outside_workspace("run_command", {"command": "cat /etc/passwd"}) is True

    def test_shell_command_with_absolute_windows_path(self):
        assert _is_outside_workspace("run_command", {"command": "type C:\\Windows\\win.ini"}) is True

    def test_shell_command_with_parent_traversal(self):
        assert _is_outside_workspace("run_command", {"command": "ls ../../"}) is True

    def test_shell_command_relative_is_safe(self):
        assert _is_outside_workspace("run_command", {"command": "ls -la"}) is False

    def test_shell_command_simple_echo(self):
        assert _is_outside_workspace("run_command", {"command": "echo hello"}) is False

    def test_path_via_path_key(self):
        assert _is_outside_workspace("edit_file", {"path": "/tmp/x"}) is True

    def test_path_via_file_key(self):
        assert _is_outside_workspace("edit_file", {"file": "/tmp/x"}) is True


# ---------------------------------------------------------------------------
# Approval gate integration tests
# ---------------------------------------------------------------------------


class TestApprovalGateEnforcement:
    """Tests that the approval gate enforces approval for dangerous ops."""

    @pytest.fixture
    def store(self, tmp_path):
        return SessionStore(tmp_path / "sessions")

    @pytest.fixture
    def context_builder(self):
        return _make_context_builder()

    def test_unlimited_auto_mode_absolute_path_requires_approval(
        self, store, context_builder
    ):
        """In unlimited + auto_mode, writing to an absolute path must
        still go through the approval handler."""
        session = store.create_session(session_id="u1")
        store.save(session)

        # A write tool
        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        # LLM returns a tool call targeting an absolute path
        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "/etc/passwd", "content": "hacked"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "u1", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=True,
        )

        # Approval handler MUST have been called (not auto-skipped)
        assert len(tracker.calls) == 1
        assert tracker.calls[0].tool_name == "write_file"

    def test_unlimited_auto_mode_parent_traversal_requires_approval(
        self, store, context_builder
    ):
        """In unlimited + auto_mode, writing with '..' must require approval."""
        session = store.create_session(session_id="u2")
        store.save(session)

        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "../../secret.txt", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "u2", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=True,
        )

        assert len(tracker.calls) == 1

    def test_unlimited_auto_mode_shell_tool_requires_approval(
        self, store, context_builder
    ):
        """In unlimited + auto_mode, shell tools must ALWAYS require approval."""
        session = store.create_session(session_id="u3")
        store.save(session)

        tools = {"run_command": _FakeTool("run_command", safety_level="shell")}
        registry = _FakeToolRegistry(tools)

        # Even a simple shell command should require approval in unlimited mode
        tool_call = MagicMock()
        tool_call.name = "run_command"
        tool_call.args = {"command": "echo hello"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "u3", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=True,
        )

        # Shell tool in unlimited mode must trigger approval even with auto_mode
        assert len(tracker.calls) == 1
        assert tracker.calls[0].tool_name == "run_command"

    def test_unlimited_auto_mode_relative_path_requires_approval(
        self, store, context_builder
    ):
        """In unlimited + auto_mode, writing to a relative path (no '..')
        still requires approval because it may resolve outside workspace."""
        session = store.create_session(session_id="u4")
        store.save(session)

        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "src/main.py", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "u4", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=True,
        )

        assert len(tracker.calls) == 1

    def test_unlimited_without_approval_channel_fails_closed(
        self, store, context_builder
    ):
        """Dangerous operations must not execute when approval is unavailable."""
        session = store.create_session(session_id="u-no-approval")
        store.save(session)

        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)
        execute_calls = []
        original_execute = registry.execute_by_name

        def _tracking_execute(name, args):
            execute_calls.append((name, args))
            return original_execute(name, args)

        registry.execute_by_name = _tracking_execute
        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "outside.txt", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        result = run_agent_turn(
            "u-no-approval", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=None,
            auto_mode=True,
            unlimited_mode=True,
        )

        assert result == "done"
        assert execute_calls == []
        saved = store.get("u-no-approval")
        assert any(
            message.role == "tool" and "必须经过用户审批" in message.content
            for message in saved.messages
        )

    def test_normal_mode_without_approval_channel_also_fails_closed(
        self, store, context_builder
    ):
        session = store.create_session(session_id="normal-no-approval")
        store.save(session)
        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)
        executed = []
        original_execute = registry.execute_by_name

        def tracking_execute(name, args):
            executed.append(name)
            return original_execute(name, args)

        registry.execute_by_name = tracking_execute
        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "local.txt", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        run_agent_turn(
            "normal-no-approval", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=None,
            auto_mode=False,
            unlimited_mode=False,
        )
        assert executed == []

    def test_non_unlimited_auto_mode_relative_path_auto_approved(
        self, store, context_builder
    ):
        """Without unlimited mode, auto_mode auto-approves write tools."""
        session = store.create_session(session_id="u5")
        store.save(session)

        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "local.txt", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "u5", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=False,
        )

        assert len(tracker.calls) == 0

    def test_sandboxed_auto_mode_absolute_path_does_not_prompt_for_approval(
        self, store, context_builder
    ):
        """The workspace-aware tool handler, not a path-string heuristic,
        decides whether an absolute path is allowed.

        Models sometimes emit an absolute path that still points inside the
        workspace.  AUTO mode must not intermittently prompt solely because
        the spelling changed from relative to absolute.
        """
        session = store.create_session(session_id="auto-absolute")
        store.save(session)
        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "/workspace/src/main.py", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])
        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "auto-absolute", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=False,
        )

        assert tracker.calls == []

    def test_non_auto_mode_always_requires_approval(
        self, store, context_builder
    ):
        """Without auto_mode, all write tools require approval."""
        session = store.create_session(session_id="u6")
        store.save(session)

        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "local.txt", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="approved")

        run_agent_turn(
            "u6", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=False,
            unlimited_mode=False,
        )

        assert len(tracker.calls) == 1

    def test_unlimited_rejection_prevents_execution(
        self, store, context_builder
    ):
        """When approval is rejected, the tool must NOT be executed."""
        session = store.create_session(session_id="u7")
        store.save(session)

        tools = {"write_file": _FakeTool("write_file", safety_level="write")}
        registry = _FakeToolRegistry(tools)

        # Track if execute was called
        original_execute = registry.execute_by_name
        execute_calls = []

        def _tracking_execute(name, args):
            execute_calls.append((name, args))
            return original_execute(name, args)

        registry.execute_by_name = _tracking_execute

        tool_call = MagicMock()
        tool_call.name = "write_file"
        tool_call.args = {"file_path": "/etc/passwd", "content": "x"}
        client = _FakeLLMClient([
            _FakeLLMResponse(is_tool_call=True, tool_calls=[tool_call]),
            _FakeLLMResponse(final="done"),
        ])

        tracker = _ApprovalCallTracker(decision="rejected")

        run_agent_turn(
            "u7", "test",
            session_store=store,
            context_builder=context_builder,
            tool_registry=registry,
            llm_client=client,
            approval_handler=tracker,
            auto_mode=True,
            unlimited_mode=True,
        )

        # Approval was called and rejected
        assert len(tracker.calls) == 1
        # Tool was NOT executed
        assert len(execute_calls) == 0


# ---------------------------------------------------------------------------
# /unlimited command registration tests
# ---------------------------------------------------------------------------


class TestUnlimitedCommandRegistration:
    """Tests that /unlimited is properly registered."""

    def test_unlimited_in_command_prefixes(self):
        from claw.cli.commands import _COMMAND_PREFIXES
        assert "/unlimited" in _COMMAND_PREFIXES

    def test_unlimited_is_command(self):
        from claw.cli.commands import is_command
        assert is_command("/unlimited") is True
        assert is_command("/unlimited on") is True
        assert is_command("/unlimited off") is True
        assert is_command("/unlimited toggle") is True

    def test_unlimited_in_help_text(self):
        from claw.cli.commands import _HELP_TEXT
        assert "/unlimited" in _HELP_TEXT

    def test_gateway_is_slash_command(self):
        from claw.gateway.server import _is_slash_command
        assert _is_slash_command("/unlimited") is True
        assert _is_slash_command("/unlimited on") is True

    def test_chat_endpoint_never_forwards_unlimited_to_llm(
        self, monkeypatch, tmp_path
    ):
        import asyncio
        import claw.gateway.server as server
        from claw.gateway.server import ChatRequest
        from claw.session.store import SessionStore

        store = SessionStore(tmp_path / "sessions")
        session = store.create_session(session_id="web-unlimited")
        store.save(session)
        monkeypatch.setattr(server, "_session_store", store)
        monkeypatch.setattr(
            server,
            "_execute_slash_command",
            lambda command, session_id: "UNLIMITED 本地命令已执行",
        )
        monkeypatch.setattr(
            server,
            "run_agent_turn",
            lambda *args, **kwargs: pytest.fail("slash command reached the LLM"),
        )

        response = asyncio.run(server.handle_chat(ChatRequest(
            sessionId="web-unlimited",
            message="/unlimited",
        )))

        assert response["type"] == "command"
        assert response["reply"] == "UNLIMITED 本地命令已执行"
        assert response["messages"][-1]["content"] == response["reply"]


# ---------------------------------------------------------------------------
# /unlimited command behavior tests
# ---------------------------------------------------------------------------


class TestUnlimitedCommandBehavior:
    """Tests for /unlimited command behavior via WorkspaceManager."""

    def test_unlimited_on(self):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.workspace.manager import WorkspaceManager

        wm = WorkspaceManager()
        store = MagicMock()
        store.create_session.return_value = MagicMock(session_id="test_ul")

        state = RuntimeState(
            session_store=store,
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="test_ul",
            workspace_manager=wm,
        )

        result = handle_command("/unlimited on", state)
        assert "UNLIMITED" in result or "已开启" in result
        assert wm.is_unlimited("test_ul") is True

    def test_unlimited_off(self):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.workspace.manager import WorkspaceManager

        wm = WorkspaceManager()
        wm.set_unlimited("test_ul_off", True)

        store = MagicMock()
        state = RuntimeState(
            session_store=store,
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="test_ul_off",
            workspace_manager=wm,
        )

        result = handle_command("/unlimited off", state)
        assert "已关闭" in result
        assert wm.is_unlimited("test_ul_off") is False

    def test_unlimited_toggle(self):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.workspace.manager import WorkspaceManager

        wm = WorkspaceManager()
        store = MagicMock()

        state = RuntimeState(
            session_store=store,
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="test_ul_tog",
            workspace_manager=wm,
        )

        # Toggle on
        result1 = handle_command("/unlimited toggle", state)
        assert wm.is_unlimited("test_ul_tog") is True

        # Toggle off
        result2 = handle_command("/unlimited toggle", state)
        assert wm.is_unlimited("test_ul_tog") is False

    def test_unlimited_no_args_shows_help_without_toggling(self):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.workspace.manager import WorkspaceManager

        wm = WorkspaceManager()
        store = MagicMock()

        state = RuntimeState(
            session_store=store,
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="test_ul_noargs",
            workspace_manager=wm,
        )

        # No args -> status/help only
        result = handle_command("/unlimited", state)
        assert wm.is_unlimited("test_ul_noargs") is False
        assert "/unlimited on" in result
        assert "/unlimited off" in result
        assert "逐次审批" in result

    def test_unlimited_status_reports_enabled_state(self):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.workspace.manager import WorkspaceManager

        wm = WorkspaceManager()
        wm.set_unlimited("test_ul_status", True)
        state = RuntimeState(
            session_store=MagicMock(),
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="test_ul_status",
            workspace_manager=wm,
        )

        result = handle_command("/unlimited status", state)
        assert "当前已开启" in result
        assert wm.is_unlimited("test_ul_status") is True


# ---------------------------------------------------------------------------
# /auto command behavior tests
# ---------------------------------------------------------------------------


class TestAutoCommandBehavior:
    """AUTO mode must only change through an explicit subcommand."""

    @staticmethod
    def _state(*, auto_mode: bool = False):
        from claw.cli.commands import RuntimeState

        return RuntimeState(
            session_store=MagicMock(),
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="auto-command-test",
            auto_mode=auto_mode,
        )

    def test_bare_auto_only_shows_status(self):
        from claw.cli.commands import handle_command

        state = self._state(auto_mode=False)
        result = handle_command("/auto", state)

        assert state.auto_mode is False
        assert "当前已关闭" in result
        assert "/auto on" in result
        assert "/auto off" in result

    def test_bare_auto_preserves_enabled_state(self):
        from claw.cli.commands import handle_command

        state = self._state(auto_mode=True)
        result = handle_command("/auto", state)

        assert state.auto_mode is True
        assert "当前已开启" in result

    def test_auto_requires_explicit_toggle(self):
        from claw.cli.commands import handle_command

        state = self._state()
        handle_command("/auto on", state)
        assert state.auto_mode is True
        handle_command("/auto off", state)
        assert state.auto_mode is False
        handle_command("/auto toggle", state)
        assert state.auto_mode is True

    def test_gateway_persists_auto_mode(self, monkeypatch):
        from claw.gateway import server

        modes: dict[str, bool] = {}
        monkeypatch.setattr(server, "_auto_mode", modes)

        server._execute_slash_command("/auto on", "auto-gateway-test")
        assert modes["auto-gateway-test"] is True

        result = server._execute_slash_command("/auto", "auto-gateway-test")
        assert "当前状态：已开启" in result
        assert modes["auto-gateway-test"] is True

        server._execute_slash_command("/auto off", "auto-gateway-test")
        assert "auto-gateway-test" not in modes


class TestCommandMarkdownOutput:
    """Gateway command output should be formatted as WebUI Markdown."""

    @staticmethod
    def _state():
        from claw.cli.commands import RuntimeState

        return RuntimeState(
            session_store=MagicMock(),
            memory_store=MagicMock(),
            llm_client=MagicMock(),
            current_session_id="markdown-command-test",
        )

    def test_help_has_markdown_sections_and_inline_commands(self):
        from claw.cli.commands import handle_command

        result = handle_command("/help", self._state(), markdown=True)

        assert result.startswith("# SJTUClaw 可用指令")
        assert "## Agent 模式" in result
        assert "## 长期记忆与反思" in result
        assert "## Skill 管理" in result
        memory_section = result.split("## 长期记忆与反思", 1)[1].split("## Workspace", 1)[0]
        skill_section = result.split("## Skill 管理", 1)[1].split("## 定时作业", 1)[0]
        assert "`/reflect status`" in memory_section
        assert "`/reflect status`" not in skill_section
        assert "- `/auto`" in result
        assert "> **安全提示：**" in result

    def test_cli_help_remains_plain_text(self):
        from claw.cli.commands import handle_command

        result = handle_command("/help", self._state())

        assert result.startswith("SJTUClaw 可用指令：")
        assert not result.startswith("# ")

    def test_terminal_style_list_is_converted_for_webui(self):
        from claw.cli.commands import _format_command_markdown

        result = _format_command_markdown(
            "待审批操作:\n  [abc] write_file session=test\n    参数: {'path': 'a'}"
        )

        assert result.startswith("### 待审批操作")
        assert "- [abc] write_file session=test" in result
        assert "  - 参数:" in result


# ---------------------------------------------------------------------------
# WorkspaceManager unlimited mode tests
# ---------------------------------------------------------------------------


class TestWorkspaceManagerUnlimited:
    """Tests for WorkspaceManager unlimited mode behavior."""

    def test_set_unlimited_on(self):
        from claw.workspace.manager import WorkspaceManager
        wm = WorkspaceManager()
        wm.set_unlimited("s1", True)
        assert wm.is_unlimited("s1") is True

    def test_set_unlimited_off(self):
        from claw.workspace.manager import WorkspaceManager
        wm = WorkspaceManager()
        wm.set_unlimited("s1", True)
        wm.set_unlimited("s1", False)
        assert wm.is_unlimited("s1") is False

    def test_default_is_not_unlimited(self):
        from claw.workspace.manager import WorkspaceManager
        wm = WorkspaceManager()
        assert wm.is_unlimited("never_set") is False

    def test_unlimited_resolve_bypasses_boundary_check(self, tmp_path):
        """In unlimited mode, resolve() should return the path as-is
        without raising WorkspaceError."""
        from claw.workspace.manager import WorkspaceManager
        wm = WorkspaceManager()
        wm.set_unlimited("s1", True)

        # An absolute path outside workspace should resolve without error
        result = wm.resolve("s1", str(tmp_path / "outside.txt"))
        assert result is not None
