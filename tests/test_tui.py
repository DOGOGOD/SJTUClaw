from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from textual.widgets import Static

from claw.cli.commands import _COMMAND_PREFIXES
from claw.tui.app import (
    CommandPanel,
    ConfirmCronDeleteScreen,
    ConfirmDeleteScreen,
    CronBoard,
    RenameSessionScreen,
    SJTUClawTUI,
    SessionBoard,
)
from claw.tui.app import COMMANDS
from claw.tui.runtime import LocalRuntime, RuntimeSnapshot


class FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.sessions = [
            {
                "sessionId": "session-alpha",
                "title": "编译器实验",
                "messageCount": 2,
                "updatedAt": "2026-07-30T10:30:00+08:00",
                "preview": "解释 SSA",
            },
            {
                "sessionId": "session-beta",
                "title": "课程报告",
                "messageCount": 0,
                "updatedAt": "2026-07-29T09:00:00+08:00",
                "preview": "",
            },
        ]
        self._messages = {
            "session-alpha": [
                {"role": "user", "content": "解释 SSA"},
                {"role": "assistant", "content": "SSA 是静态单赋值形式。"},
            ],
            "session-beta": [],
        }
        self.snapshot_value = RuntimeSnapshot(
            session_id="session-alpha",
            title="编译器实验",
            backend="sjtuclaw",
            model="gpt-test",
            workspace="C:/workspace",
            auto_mode=False,
            sandbox_mode=True,
            unlimited_mode=False,
        )

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    def ensure_session(self) -> str:
        return "session-alpha"

    def list_sessions(self):
        return list(self.sessions)

    def messages(self, session_id: str):
        return list(self._messages[session_id])

    def snapshot(self, session_id: str) -> RuntimeSnapshot:
        title = next(item["title"] for item in self.sessions if item["sessionId"] == session_id)
        return replace(self.snapshot_value, session_id=session_id, title=title)

    def cron_jobs(self):
        return [
            {
                "id": "job-1",
                "name": "每日回顾",
                "enabled": True,
                "system": False,
                "schedule": "0 22 * * * · Asia/Shanghai",
                "nextRun": "07-30 22:00",
                "lastStatus": "ok",
                "message": "复盘今天的工作",
            }
        ]

    def pending_approvals(self, session_id: str):
        return []

    async def stream(self, session_id: str, message: str):
        yield {"type": "ThinkingEvent", "iteration": 1}
        yield {
            "type": "ToolCallStartEvent",
            "call_id": "call-1",
            "tool_name": "readonly",
            "args": {"path": "README.md"},
        }
        yield {
            "type": "ToolCallEndEvent",
            "call_id": "call-1",
            "tool_name": "readonly",
            "ok": True,
            "result": "读取完成",
            "duration_ms": 25,
        }
        self._messages[session_id].extend(
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "收到。"},
            ]
        )
        yield {"type": "FinalEvent", "content": "收到。"}
        yield {"type": "_session_info", "sessionId": session_id}
        yield {"type": "_done"}

    async def command(self, session_id: str, command: str):
        if command.startswith("/session switch "):
            return {
                "result": f"执行：{command}",
                "actions": ["switch_session"],
                "switchToSessionId": command.split(maxsplit=2)[2],
            }
        return {"result": f"执行：{command}", "actions": []}

    def stop(self, session_id: str):
        return "当前没有正在运行的任务"

    def approve(self, approval_id: str):
        return True

    def reject(self, approval_id: str, reason: str = ""):
        return True

    async def trigger_cron(self, job_id: str):
        return True


def test_tui_command_atlas_covers_every_cli_namespace() -> None:
    assert {command for command, _description in COMMANDS} == set(_COMMAND_PREFIXES)


def test_tui_mounts_transcript_first_cockpit_without_session_sidebar() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        app = SJTUClawTUI(runtime=runtime)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            assert runtime.started
            assert app.query_one("#composer").has_focus
            assert len(app.query(".message-card")) == 2
            assert len(app.query("#session-rail")) == 0
            assert app.query_one("#conversation").size.width > 100
            assert "gpt-test" in str(app.query_one("#runtime-card", Static).render())
            assert "◈" not in str(app.query_one("#brand-header", Static).render())
            assert (
                app.query_one("#composer-shell").region.right
                == app.query_one("#insight-rail").region.x
            )
            transcript = app.query_one("#transcript")
            insight_rail = app.query_one("#insight-rail")
            message = app.query_one(".message-card")
            assert transcript.region.right == insight_rail.region.x
            assert insight_rail.region.x - message.region.right <= 1
            assert "F1" not in str(app.query_one("#keybar", Static).render())
        assert runtime.closed

    asyncio.run(scenario())


def test_tui_command_hints_and_responsive_layout() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(72, 26)) as pilot:
            await pilot.press("/", "c", "r", "o")
            await pilot.pause()
            hints = app.query_one("#command-hints", Static)
            assert hints.display
            assert "/cron" in str(hints.render())
            await pilot.press("tab")
            assert app.query_one("#composer").text == "/cron "
            assert app.screen.has_class("narrow")
            assert app.screen.has_class("short")

    asyncio.run(scenario())


def test_shift_enter_inserts_newline_without_submitting() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        app = SJTUClawTUI(runtime=runtime)
        async with app.run_test(size=(120, 36)) as pilot:
            initial_messages = len(runtime.messages("session-alpha"))
            await pilot.press("第", "一", "行", "shift+enter", "第", "二", "行")
            await pilot.pause()

            assert app.query_one("#composer").text == "第一行\n第二行"
            assert len(runtime.messages("session-alpha")) == initial_messages
            assert not app.busy

    asyncio.run(scenario())


def test_windows_shift_enter_keeps_modifier_before_textual_parsing() -> None:
    if __import__("os").name != "nt":
        return
    from claw.tui.windows_driver import (
        SHIFT_PRESSED,
        VK_RETURN,
        translate_windows_key,
    )

    assert translate_windows_key("\r", SHIFT_PRESSED, VK_RETURN) == "\x1b[13;2u"
    assert translate_windows_key("\r", 0, VK_RETURN) == "\r"


def test_tui_inline_command_palette_scrolls_through_every_command() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.press("/")
            await pilot.pause()
            hints = app.query_one("#command-hints", Static)
            assert len(app._command_matches) == len(COMMANDS)
            assert hints.region.right == app.query_one("#insight-rail").region.x
            assert "↓ 13 条命令" in str(hints.render())

            await pilot.press(*(["down"] * 9))
            await pilot.pause()
            assert app.query_one("#composer").completion == "/skill"
            rendered = str(hints.render())
            assert "/skill" in rendered
            assert "↑ " in rendered

            await pilot.press("tab")
            assert app.query_one("#composer").text == "/skill "

    asyncio.run(scenario())


def test_ctrl_p_opens_sjtuclaw_command_panel_and_inserts_selection() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(120, 36)) as pilot:
            assert not app.use_command_palette
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert isinstance(app.screen, CommandPanel)
            first_panel = app.screen
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert app.screen is first_panel
            table = app.screen.query_one("#command-table")
            assert table.row_count == len(COMMANDS)
            assert all(
                table.get_row_at(index)[0] != "Theme"
                for index in range(table.row_count)
            )

            await pilot.press("c", "r", "o", "n")
            await pilot.pause()
            assert app.screen.query_one("#command-table").row_count == 1
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, CommandPanel)
            assert app.query_one("#composer").text == "/cron "
            assert app.query_one("#composer").has_focus

    asyncio.run(scenario())


def test_command_panel_preserves_existing_draft_instead_of_corrupting_it() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("保", "留", "草", "稿", "ctrl+p")
            await pilot.pause()
            await pilot.press("c", "r", "o", "n", "enter")
            await pilot.pause()

            assert app.query_one("#composer").text == "保留草稿"
            assert any(
                notification.message
                == "已有草稿，命令未插入；请发送或清空草稿后重试。"
                for notification in app._notifications
            )

    asyncio.run(scenario())


def test_busy_turn_preserves_new_draft_on_enter() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(120, 36)) as pilot:
            app.busy = True
            await pilot.press("不", "要", "丢", "失", "enter")
            await pilot.pause()

            assert app.query_one("#composer").text == "不要丢失"
            assert any(
                notification.message == "当前任务仍在运行；草稿已保留。可按 Ctrl+C 停止。"
                for notification in app._notifications
            )

    asyncio.run(scenario())


def test_busy_approvals_command_does_not_cancel_or_replace_live_turn() -> None:
    class SlowRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def stream(self, session_id: str, message: str):
            yield {"type": "ThinkingEvent", "iteration": 1}
            await self.release.wait()
            self._messages[session_id].extend(
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "完成。"},
                ]
            )
            yield {"type": "_done"}

    async def scenario() -> None:
        runtime = SlowRuntime()
        app = SJTUClawTUI(runtime=runtime)
        async with app.run_test(size=(120, 36)) as pilot:
            turn_worker = app.execute_turn("长任务")
            await pilot.pause()
            assert app.busy

            command_worker = app.execute_command("/approvals")
            await command_worker.wait()
            await pilot.pause()
            assert app.busy
            assert any(
                message.get("content") == "长任务"
                for message in app._ephemeral_messages
            )

            runtime.release.set()
            await turn_worker.wait()

    asyncio.run(scenario())


def test_only_ctrl_j_opens_cron_and_ctrl_m_does_not() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(120, 36)) as pilot:
            assert not app.query_one("#open-cron").can_focus

            await pilot.press("ctrl+m")
            await pilot.pause()
            assert not isinstance(app.screen, CronBoard)

            await pilot.press("ctrl+j")
            await pilot.pause()
            assert isinstance(app.screen, CronBoard)

    asyncio.run(scenario())


def test_ctrl_c_uses_short_deduplicated_status_text() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()

            notifications = list(app._notifications)
            assert len(notifications) == 1
            assert notifications[0].message == "当前没有运行中的任务。"
            assert "`" not in notifications[0].message

    asyncio.run(scenario())


def test_tui_switches_sessions_only_through_ctrl_s_picker() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SessionBoard)
            await pilot.press("down", "enter")
            await pilot.pause()
            assert app.current_session_id == "session-beta"
            assert len(app.query(".message-card")) == 0
            assert app.query_one("#welcome-panel") is not None

    asyncio.run(scenario())


def test_tui_opens_each_keyboard_dashboard() -> None:
    async def scenario() -> None:
        app = SJTUClawTUI(runtime=FakeRuntime())
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SessionBoard)
            assert app.screen.query_one("#session-table").row_count == 2
            await pilot.press("/")
            await pilot.press("课", "程")
            await pilot.pause()
            assert app.screen.query_one("#session-table").row_count == 1
            await pilot.press("escape")
            assert not isinstance(app.screen, SessionBoard)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, RenameSessionScreen)
            await pilot.press("escape")
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDeleteScreen)
            assert app.screen.query_one("#dialog-cancel").has_focus
            await pilot.press("escape")
            await pilot.press("escape")

            await pilot.press("ctrl+j")
            await pilot.pause()
            assert isinstance(app.screen, CronBoard)
            assert app.screen.query_one("#cron-table").row_count == 1
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmCronDeleteScreen)
            assert app.screen.query_one("#dialog-cancel").has_focus
            await pilot.press("escape")
            await pilot.press("escape")

    asyncio.run(scenario())


def test_tui_streams_turn_events_and_finishes_cleanly() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        app = SJTUClawTUI(runtime=runtime)
        async with app.run_test(size=(120, 36)) as pilot:
            worker = app.execute_turn("读取 README")
            await worker.wait()
            await pilot.pause()
            assert not app.busy
            assert app._ephemeral_messages == []
            assert runtime.messages("session-alpha")[-1]["content"] == "收到。"
            assert len(app.query(".message-card")) == 4

    asyncio.run(scenario())


def test_transcript_reuses_unchanged_message_widgets() -> None:
    async def scenario() -> None:
        runtime = FakeRuntime()
        app = SJTUClawTUI(runtime=runtime)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            first_card = app.query_one("#message-0")

            await app._render_transcript()
            assert app.query_one("#message-0") is first_card

            runtime._messages["session-alpha"].append(
                {"role": "assistant", "content": "只追加尾部。"}
            )
            await app._render_transcript()
            assert app.query_one("#message-0") is first_card
            assert len(app.query(".message-card")) == 3

    asyncio.run(scenario())


def test_stream_never_renders_persisted_and_ephemeral_message_twice() -> None:
    app = SJTUClawTUI(runtime=FakeRuntime())
    app._turn_base_message_count = 2
    app._ephemeral_messages = [
        {"role": "user", "content": "不会重复"},
        {"role": "tool", "content": "仍在运行"},
    ]
    persisted = [
        {"role": "user", "content": "解释 SSA"},
        {"role": "assistant", "content": "SSA 是静态单赋值形式。"},
        {"role": "user", "content": "不会重复"},
        {"role": "assistant", "content": "收到。"},
    ]

    remaining = app._unpersisted_ephemeral_messages(persisted)

    assert ("user", "不会重复") not in {
        (message["role"], message["content"]) for message in remaining
    }
    assert remaining == [{"role": "tool", "content": "仍在运行"}]


def test_runtime_start_rolls_back_cron_when_reflection_fails() -> None:
    class CronService:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self, *, loop) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    class ReflectionService:
        def start(self) -> None:
            raise RuntimeError("reflection failed")

    async def scenario() -> None:
        cron = CronService()
        runtime = LocalRuntime.__new__(LocalRuntime)
        runtime.server = SimpleNamespace(
            _cron_service=cron,
            _reflection_mgr=ReflectionService(),
        )
        runtime._started = False

        try:
            await runtime.start()
        except RuntimeError as exc:
            assert str(exc) == "reflection failed"
        else:
            raise AssertionError("runtime.start() should fail")

        assert cron.started
        assert cron.stopped
        assert not runtime._started

    asyncio.run(scenario())


def test_runtime_close_attempts_every_cleanup_after_one_failure() -> None:
    calls: list[str] = []

    class CronService:
        def stop(self) -> None:
            calls.append("cron")
            raise RuntimeError("cron failed")

    class ReflectionService:
        def stop(self) -> None:
            calls.append("reflection")

    class SandboxManager:
        def close_all(self) -> None:
            calls.append("sandbox")

    async def scenario() -> None:
        runtime = LocalRuntime.__new__(LocalRuntime)
        runtime.server = SimpleNamespace(
            _cron_service=CronService(),
            _reflection_mgr=ReflectionService(),
            _sandbox_manager=SandboxManager(),
        )
        runtime._started = True

        await runtime.close()

        assert calls == ["cron", "reflection", "sandbox"]
        assert not runtime._started

    asyncio.run(scenario())
