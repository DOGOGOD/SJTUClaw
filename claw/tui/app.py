"""Mature full-screen terminal interface for SJTUClaw."""

from __future__ import annotations

from collections import Counter
import json
import os
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import (
    Center,
    Horizontal,
    Vertical,
    VerticalScroll,
)
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Static,
)

from claw.tui.quotes import random_quote
from claw.tui.runtime import LocalRuntime
from claw.tui.widgets import (
    ApprovalCard,
    BrandHeader,
    Composer,
    MessageCard,
    WelcomePanel,
)


COMMANDS: tuple[tuple[str, str], ...] = (
    ("/session", "创建、切换、重命名或删除 Session"),
    ("/memory", "管理长期记忆与检索"),
    ("/compact", "立即压缩当前上下文"),
    ("/workspace", "绑定、查看或解除工作区"),
    ("/sandbox", "查看或切换 microVM 沙箱"),
    ("/rollback", "预览、执行或撤销文件回退"),
    ("/approvals", "查看等待中的工具审批"),
    ("/approve", "批准指定工具操作"),
    ("/reject", "拒绝指定工具操作"),
    ("/skill", "浏览并调用 Skills"),
    ("/reflect", "查看配置或执行记忆反思"),
    ("/cron", "管理定时作业"),
    ("/pet", "管理 SJTUClaw 桌面宠物"),
    ("/auto", "切换 Session 级自动审批"),
    ("/unlimited", "切换工作区边界"),
    ("/pi", "接入或退出 Pi Agent 后端"),
    ("/claude", "接入或退出 Claude Code 后端"),
    ("/stop", "停止当前 Agent 回合"),
    ("/help", "显示完整命令帮助"),
    ("/exit", "退出 TUI"),
)

COMMAND_WINDOW_SIZE = 7


class SessionBoard(ModalScreen[str | None]):
    """Dense, keyboard-first session operations board."""

    BINDINGS = [
        Binding("escape", "dismiss", "关闭"),
        Binding("q", "dismiss", "关闭"),
        Binding("n", "new", "新建"),
        Binding("e", "rename", "重命名"),
        Binding("x", "delete", "删除"),
        Binding("slash", "focus_search", "搜索"),
        Binding("j", "cursor_down", "下一项", show=False),
        Binding("k", "cursor_up", "上一项", show=False),
        Binding("r", "refresh", "刷新"),
    ]

    def __init__(self, runtime: LocalRuntime, active_id: str) -> None:
        super().__init__()
        self.runtime = runtime
        self.active_id = active_id

    def compose(self) -> ComposeResult:
        with Vertical(id="board"):
            with Horizontal(classes="board-heading"):
                yield Label("SESSION BOARD", classes="board-title")
                yield Label("/ 搜索 · J/K 选择 · Enter 切换", classes="board-kicker")
            with Horizontal(id="session-search-row"):
                yield Label("/", id="session-search-mark")
                yield Input(
                    placeholder="搜索标题、最近消息或 Session ID",
                    id="session-search",
                )
            yield DataTable(id="session-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="board-actions"):
                yield Button("新建 Session", id="session-new", variant="primary")
                yield Button("重命名", id="session-rename")
                yield Button("删除", id="session-delete", variant="error")
                yield Button("刷新", id="session-refresh")
                yield Button("关闭", id="board-close")

    def on_mount(self) -> None:
        table = self.query_one("#session-table", DataTable)
        table.add_columns("", "标题", "最近消息", "消息", "最近更新")
        self._populate()
        table.focus()

    def _populate(self) -> None:
        table = self.query_one("#session-table", DataTable)
        table.clear()
        query = self.query_one("#session-search", Input).value.strip().lower()
        for item in self.runtime.list_sessions():
            sid = item["sessionId"]
            searchable = " ".join(
                (sid, str(item["title"]), str(item.get("preview", "")))
            ).lower()
            if query and query not in searchable:
                continue
            table.add_row(
                "●" if sid == self.active_id else " ",
                item["title"],
                item.get("preview", "") or "—",
                str(item["messageCount"]),
                str(item["updatedAt"]).replace("T", " ")[:16],
                key=sid,
            )

    @on(DataTable.RowSelected, "#session-table")
    def select_session(self, event: DataTable.RowSelected) -> None:
        self.dismiss(f"/session switch {event.row_key.value}")

    def action_new(self) -> None:
        self.dismiss("/session new")

    def action_refresh(self) -> None:
        self._populate()

    def action_focus_search(self) -> None:
        self.query_one("#session-search", Input).focus()

    def action_cursor_down(self) -> None:
        self.query_one("#session-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#session-table", DataTable).action_cursor_up()

    @on(Input.Changed, "#session-search")
    def filter_sessions(self) -> None:
        self._populate()

    @on(Input.Submitted, "#session-search")
    def finish_search(self) -> None:
        self.query_one("#session-table", DataTable).focus()

    def _selected_session(self) -> tuple[str, str] | None:
        table = self.query_one("#session-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        session = next(
            (item for item in self.runtime.list_sessions() if item["sessionId"] == row_key),
            None,
        )
        return (row_key, session["title"]) if session else None

    def action_rename(self) -> None:
        selected = self._selected_session()
        if selected:
            sid, title = selected
            self.app.push_screen(
                RenameSessionScreen(sid, title),
                self._finish_child_action,
            )

    def action_delete(self) -> None:
        selected = self._selected_session()
        if selected:
            sid, title = selected
            self.app.push_screen(
                ConfirmDeleteScreen(sid, title, sid == self.active_id),
                self._finish_child_action,
            )

    def _finish_child_action(self, result: str | None) -> None:
        if result:
            self.dismiss(result)

    @on(Button.Pressed)
    def board_button(self, event: Button.Pressed) -> None:
        if event.button.id == "session-new":
            self.action_new()
        elif event.button.id == "session-rename":
            self.action_rename()
        elif event.button.id == "session-delete":
            self.action_delete()
        elif event.button.id == "session-refresh":
            self.action_refresh()
        elif event.button.id == "board-close":
            self.dismiss(None)


class RenameSessionScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss", "取消")]

    def __init__(self, session_id: str, title: str) -> None:
        super().__init__()
        self.session_id = session_id
        self.current_title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-card"):
            yield Label("重命名 SESSION", classes="dialog-title")
            yield Static(self.session_id, classes="dialog-caption")
            yield Input(value=self.current_title, id="rename-input", select_on_focus=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("保存", id="rename-save", variant="primary")
                yield Button("取消", id="dialog-cancel")

    def on_mount(self) -> None:
        field = self.query_one("#rename-input", Input)
        field.focus()
        field.action_end()

    def _save(self) -> None:
        title = self.query_one("#rename-input", Input).value.strip()
        if title:
            self.dismiss(f"/session rename {self.session_id} {title}")

    @on(Input.Submitted, "#rename-input")
    def submit_title(self) -> None:
        self._save()

    @on(Button.Pressed)
    def dialog_button(self, event: Button.Pressed) -> None:
        if event.button.id == "rename-save":
            self._save()
        elif event.button.id == "dialog-cancel":
            self.dismiss(None)


class ConfirmDeleteScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss", "取消")]

    def __init__(self, session_id: str, title: str, active: bool) -> None:
        super().__init__()
        self.session_id = session_id
        self.session_title = title
        self.active = active

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-card danger-dialog"):
            yield Label("删除 SESSION？", classes="dialog-title danger-title")
            yield Static(
                f"{self.session_title}\n{self.session_id}",
                classes="dialog-caption",
            )
            yield Static(
                "聊天记录及关联的 Workspace / Sandbox 状态将被清理。"
                + ("\n当前 Session 删除后会自动切换。" if self.active else ""),
                classes="dialog-copy",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("确认删除", id="delete-confirm", variant="error")
                yield Button("取消", id="dialog-cancel", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#dialog-cancel", Button).focus()

    @on(Button.Pressed)
    def dialog_button(self, event: Button.Pressed) -> None:
        if event.button.id == "delete-confirm":
            self.dismiss(f"/session delete {self.session_id}")
        elif event.button.id == "dialog-cancel":
            self.dismiss(None)


class CronBoard(ModalScreen[str | None]):
    """Operational cron dashboard with direct actions."""

    BINDINGS = [
        Binding("escape", "dismiss", "关闭"),
        Binding("r", "refresh", "刷新"),
        Binding("space", "toggle", "启用/禁用"),
        Binding("x", "delete", "删除"),
        Binding("enter", "run", "立即运行"),
    ]

    def __init__(self, runtime: LocalRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        with Vertical(id="board"):
            with Horizontal(classes="board-heading"):
                yield Label("CRON BOARD", classes="board-title")
                yield Label("Enter 运行 · Space 切换 · X 删除", classes="board-kicker")
            yield DataTable(id="cron-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="board-actions"):
                yield Button("立即运行", id="cron-run", variant="primary")
                yield Button("启用 / 禁用", id="cron-toggle")
                yield Button("删除", id="cron-delete", variant="error")
                yield Button("关闭", id="board-close")

    def on_mount(self) -> None:
        table = self.query_one("#cron-table", DataTable)
        table.add_columns("状态", "名称", "计划", "下次运行", "上次", "Job ID")
        self._populate()
        table.focus()

    def _populate(self) -> None:
        table = self.query_one("#cron-table", DataTable)
        table.clear()
        for job in self.runtime.cron_jobs():
            table.add_row(
                "● ON" if job["enabled"] else "○ OFF",
                ("◆ " if job["system"] else "") + job["name"],
                job["schedule"],
                job["nextRun"],
                job["lastStatus"],
                job["id"],
                key=job["id"],
            )

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#cron-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((job for job in self.runtime.cron_jobs() if job["id"] == row_key), None)

    def action_refresh(self) -> None:
        self._populate()

    def action_toggle(self) -> None:
        job = self._selected()
        if job:
            action = "disable" if job["enabled"] else "enable"
            self.dismiss(f"/cron {action} {job['id']}")

    def action_delete(self) -> None:
        job = self._selected()
        if job and not job["system"]:
            self.dismiss(f"/cron delete {job['id']}")

    def action_run(self) -> None:
        job = self._selected()
        if job:
            self.dismiss(f"::cron-run {job['id']}")

    @on(Button.Pressed)
    def board_button(self, event: Button.Pressed) -> None:
        actions = {
            "cron-run": self.action_run,
            "cron-toggle": self.action_toggle,
            "cron-delete": self.action_delete,
            "board-close": lambda: self.dismiss(None),
        }
        action = actions.get(event.button.id or "")
        if action:
            action()


class CommandPanel(ModalScreen[str | None]):
    """Searchable SJTUClaw command palette."""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    def compose(self) -> ComposeResult:
        with Vertical(id="command-panel"):
            with Horizontal(classes="board-heading"):
                yield Label("COMMAND PANEL", classes="board-title")
                yield Label("输入搜索 · ↑/↓ 选择 · Enter 插入", classes="board-kicker")
            with Horizontal(id="command-search-row"):
                yield Label("⌕", id="command-search-mark")
                yield Input(
                    placeholder="搜索 SJTUClaw 命令",
                    id="command-search",
                )
            yield DataTable(id="command-table", cursor_type="row", zebra_stripes=False)

    def on_mount(self) -> None:
        table = self.query_one("#command-table", DataTable)
        table.add_columns("命令", "功能")
        self._populate()
        self.query_one("#command-search", Input).focus()

    def _populate(self) -> None:
        table = self.query_one("#command-table", DataTable)
        table.clear()
        query = self.query_one("#command-search", Input).value.strip().lower()
        for command, description in COMMANDS:
            if query and query not in f"{command} {description}".lower():
                continue
            table.add_row(command, description, key=command)

    def _selected_command(self) -> str | None:
        table = self.query_one("#command-table", DataTable)
        if table.row_count == 0:
            return None
        return str(
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        )

    def on_key(self, event: events.Key) -> None:
        table = self.query_one("#command-table", DataTable)
        if event.key == "down":
            event.prevent_default()
            event.stop()
            table.action_cursor_down()
        elif event.key == "up":
            event.prevent_default()
            event.stop()
            table.action_cursor_up()

    @on(Input.Changed, "#command-search")
    def filter_commands(self) -> None:
        self._populate()

    @on(Input.Submitted, "#command-search")
    def choose_from_search(self) -> None:
        command = self._selected_command()
        if command:
            self.dismiss(command)

    @on(DataTable.RowSelected, "#command-table")
    def choose_from_table(self, event: DataTable.RowSelected) -> None:
        self.dismiss(str(event.row_key.value))


class RailActionButton(Button):
    """Mouse-only rail action so Enter / Ctrl+M can't trigger it."""

    can_focus = False


class SJTUClawTUI(App[None]):
    """SJTUClaw's keyboard-first terminal cockpit."""

    CSS_PATH = "sjtuclaw.tcss"
    TITLE = "SJTUClaw"
    SUB_TITLE = "Terminal Agent"
    # The built-in Textual palette adds unrelated actions such as Theme and
    # competes with our Ctrl+P binding. SJTUClaw owns the palette end-to-end.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+p", "command_panel", "命令面板", show=True, priority=True),
        Binding("ctrl+s", "sessions", "Sessions", show=True),
        Binding("ctrl+j", "cron", "Cron", show=True),
        Binding("ctrl+r", "refresh", "刷新", show=True),
        Binding("ctrl+c", "stop_turn", "停止", show=True, priority=True),
        Binding("escape", "escape", "返回", show=False),
        Binding("ctrl+q", "quit", "退出", show=False),
    ]

    current_session_id = reactive("")
    busy = reactive(False)

    def __init__(self, runtime: LocalRuntime | None = None) -> None:
        super().__init__()
        self.runtime = runtime or LocalRuntime()
        self.current_session_id = self.runtime.ensure_session()
        self._ephemeral_messages: list[dict[str, Any]] = []
        self._turn_base_message_count: int | None = None
        self._command_matches: list[tuple[str, str]] = []
        self._command_index = 0

    def get_driver_class(self):
        if os.name == "nt":
            from claw.tui.windows_driver import SJTUClawWindowsDriver

            return SJTUClawWindowsDriver
        return super().get_driver_class()

    def compose(self) -> ComposeResult:
        yield BrandHeader(id="brand-header")
        with Horizontal(id="top-status"):
            yield Static("● READY", id="run-state", classes="status-ready")
            yield Static("", id="session-title")
            yield Static("", id="backend-pill", classes="mode-pill")
            yield Static("", id="safety-pill", classes="mode-pill")
        with Horizontal(id="workspace-shell"):
            with Vertical(id="conversation"):
                yield VerticalScroll(id="transcript")
                yield Static("", id="command-hints")
                with Horizontal(id="composer-shell"):
                    yield Static("›", id="prompt-mark")
                    yield Composer(id="composer", language=None, soft_wrap=True)
                    yield Static("↵ 发送\n⇧↵ 换行", id="send-hint")
                yield Static("", id="composer-meta")
            with Vertical(id="insight-rail"):
                yield Label("RUNTIME", classes="rail-title")
                yield Static("", id="runtime-card", classes="insight-card")
                with Horizontal(classes="rail-title-row section-gap"):
                    yield Label("CRON", classes="rail-title")
                    yield RailActionButton("↗", id="open-cron", classes="icon-button")
                yield Static("", id="cron-glance", classes="insight-card")
                yield Label("APPROVALS", classes="rail-title section-gap")
                yield Vertical(id="approval-list")
        yield Static(
            "^P commands   ^S sessions   ^J cron   / inline",
            id="keybar",
        )

    async def on_mount(self) -> None:
        await self.runtime.start()
        self._apply_responsive_classes(self.size.width, self.size.height)
        self.query_one("#composer", Composer).focus()
        self.set_interval(0.75, self._poll_live_state)
        self.set_interval(5.0, self._refresh_background)
        await self.refresh_all()

    async def on_unmount(self) -> None:
        await self.runtime.close()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_classes(event.size.width, event.size.height)

    def _apply_responsive_classes(self, width: int, height: int) -> None:
        self.screen.set_class(width <= 110, "compact")
        self.screen.set_class(width <= 78, "narrow")
        self.screen.set_class(height <= 28, "short")

    async def refresh_all(self, *, keep_ephemeral: bool = False) -> None:
        if not keep_ephemeral:
            self._ephemeral_messages.clear()
        self._refresh_status()
        await self._render_transcript()
        self._refresh_approvals()

    def _refresh_status(self) -> None:
        snapshot = self.runtime.snapshot(self.current_session_id)
        self.query_one("#session-title", Static).update(snapshot.title)
        backend_labels = {
            "sjtuclaw": "◈ NATIVE",
            "pi": "π PI",
            "claude": "✦ CLAUDE",
        }
        self.query_one("#backend-pill", Static).update(
            backend_labels.get(snapshot.backend, snapshot.backend.upper())
        )
        safety: list[str] = []
        if snapshot.sandbox_mode:
            safety.append("SANDBOX")
        if snapshot.auto_mode:
            safety.append("AUTO")
        if snapshot.unlimited_mode:
            safety.append("UNLIMITED")
        self.query_one("#safety-pill", Static).update(" · ".join(safety) or "GUARDED")
        state = self.query_one("#run-state", Static)
        state.update("◌ WORKING" if self.busy else "● READY")
        state.set_classes("status-busy" if self.busy else "status-ready")
        self.query_one("#runtime-card", Static).update(
            Text.assemble(
                ("MODEL\n", "bold #756e79"),
                (f"{snapshot.model}\n\n", "#eee8e2"),
                ("WORKSPACE\n", "bold #756e79"),
                (f"{snapshot.workspace}\n\n", "#c9c1ca"),
                ("BACKEND\n", "bold #756e79"),
                (snapshot.backend.upper(), "#e8ad52"),
            )
        )
        jobs = self.runtime.cron_jobs()
        lines: list[Text] = []
        for job in jobs[:4]:
            line = Text()
            line.append("● " if job["enabled"] else "○ ", style="#4bc69b" if job["enabled"] else "#655e69")
            line.append(job["name"][:20], style="#e8e2dc")
            line.append(f"\n  {job['nextRun']}", style="#766f7a")
            lines.append(line)
        self.query_one("#cron-glance", Static).update(
            Text("\n\n").join(lines) if lines else Text("暂无定时作业\n\nCtrl+J 打开看板", style="#766f7a")
        )
        self.query_one("#composer-meta", Static).update(
            f"{snapshot.backend} · {snapshot.workspace} · "
            f"{'sandbox' if snapshot.sandbox_mode else 'host'}"
        )

    async def _render_transcript(self) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        persisted = self.runtime.messages(self.current_session_id)
        ephemeral = self._unpersisted_ephemeral_messages(persisted)
        messages = persisted + ephemeral
        visible = [
            message
            for message in messages
            if message.get("role") in {"user", "assistant", "tool", "system"}
            and (message.get("content") or message.get("tool_calls"))
        ]
        if not visible:
            quote, author = random_quote()
            await transcript.mount(WelcomePanel(quote, author))
        else:
            cards = [MessageCard(message, index) for index, message in enumerate(visible)]
            await transcript.mount(*cards)
        transcript.scroll_end(animate=False)

    def _unpersisted_ephemeral_messages(
        self,
        persisted: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Hide local turn copies as soon as Gateway persistence catches up."""
        if self._turn_base_message_count is None:
            return list(self._ephemeral_messages)

        recent = persisted[self._turn_base_message_count :]
        persisted_counts = Counter(
            (str(message.get("role")), str(message.get("content") or ""))
            for message in recent
        )
        remaining: list[dict[str, Any]] = []
        for message in self._ephemeral_messages:
            identity = (
                str(message.get("role")),
                str(message.get("content") or ""),
            )
            if persisted_counts[identity]:
                persisted_counts[identity] -= 1
            else:
                remaining.append(message)
        return remaining

    def _refresh_approvals(self) -> None:
        self._replace_approval_cards(self.runtime.pending_approvals(self.current_session_id))

    @work(exclusive=True, group="approval-render")
    async def _replace_approval_cards(self, approvals: list[dict[str, Any]]) -> None:
        container = self.query_one("#approval-list", Vertical)
        await container.remove_children()
        if approvals:
            await container.mount(*(ApprovalCard(item) for item in approvals))
        else:
            await container.mount(Static("没有等待中的操作", classes="muted"))

    async def _poll_live_state(self) -> None:
        if self.busy:
            self._refresh_approvals()
            self._refresh_status()

    async def _refresh_background(self) -> None:
        if not self.busy:
            self._refresh_status()
            self._refresh_approvals()

    @on(Composer.Submitted)
    async def submit_composer(self, event: Composer.Submitted) -> None:
        composer = self.query_one("#composer", Composer)
        composer.text = ""
        self.query_one("#command-hints", Static).display = False
        if event.value == "/exit":
            self.exit()
            return
        if event.value.startswith("/"):
            self.execute_command(event.value)
        else:
            self.execute_turn(event.value)

    @work(exclusive=True, group="turn")
    async def execute_turn(self, message: str) -> None:
        if self.busy:
            self.notify("当前 Session 仍在运行，可按 Ctrl+C 停止。", severity="warning")
            return
        self.busy = True
        self._turn_base_message_count = len(
            self.runtime.messages(self.current_session_id)
        )
        self._ephemeral_messages = [{"role": "user", "content": message}]
        await self._render_transcript()
        try:
            async for event in self.runtime.stream(self.current_session_id, message):
                event_type = event.get("type")
                if event_type == "ThinkingEvent":
                    self._set_live_activity(
                        {
                            "role": "system",
                            "content": f"◌ 正在思考 · iteration {event.get('iteration', 0)}",
                            "_live": True,
                        }
                    )
                elif event_type == "ToolCallStartEvent":
                    args = json.dumps(event.get("args", {}), ensure_ascii=False, indent=2)
                    self._set_live_activity(
                        {
                            "role": "tool",
                            "name": event.get("tool_name", "tool"),
                            "content": f"运行中…\n{args}",
                            "call_id": event.get("call_id"),
                            "_live": True,
                        }
                    )
                elif event_type == "ToolCallEndEvent":
                    outcome = event.get("result") if event.get("ok", True) else event.get("error")
                    outcome_text = str(outcome or "完成")
                    if len(outcome_text) > 1600:
                        outcome_text = outcome_text[:1600].rstrip() + "\n…"
                    self._complete_tool_activity(
                        str(event.get("call_id") or ""),
                        str(event.get("tool_name") or "tool"),
                        bool(event.get("ok", True)),
                        outcome_text,
                        float(event.get("duration_ms") or 0),
                    )
                elif event_type == "ErrorEvent":
                    self._ephemeral_messages.append(
                        {
                            "role": "system",
                            "content": f"**运行警告**\n\n{event.get('error', '未知错误')}",
                        }
                    )
                elif event_type == "_session_info":
                    self.current_session_id = event.get("sessionId", self.current_session_id)
                elif event_type == "_title":
                    self.query_one("#session-title", Static).update(event.get("title", ""))
                elif event_type == "_done":
                    break
                await self._render_transcript()
            self._ephemeral_messages.clear()
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            self._ephemeral_messages.append(
                {"role": "assistant", "content": f"**运行失败**\n\n{detail}"}
            )
            self.notify(str(detail), severity="error")
        finally:
            self.busy = False
            self._ephemeral_messages = self._unpersisted_ephemeral_messages(
                self.runtime.messages(self.current_session_id)
            )
            await self.refresh_all(keep_ephemeral=bool(self._ephemeral_messages))
            self._turn_base_message_count = None
            self.query_one("#composer", Composer).focus()

    def _set_live_activity(self, activity: dict[str, Any]) -> None:
        self._ephemeral_messages = [
            message for message in self._ephemeral_messages if not message.get("_live")
        ]
        self._ephemeral_messages.append(activity)

    def _complete_tool_activity(
        self,
        call_id: str,
        tool_name: str,
        ok: bool,
        outcome: str,
        duration_ms: float,
    ) -> None:
        for message in reversed(self._ephemeral_messages):
            if message.get("call_id") == call_id:
                message["_live"] = False
                message["content"] = (
                    f"{'✓' if ok else '✕'} {outcome}\n"
                    f"完成于 {duration_ms / 1000:.2f}s"
                )
                return
        self._ephemeral_messages.append(
            {
                "role": "tool",
                "name": tool_name,
                "content": f"{'✓' if ok else '✕'} {outcome}",
            }
        )

    @work(exclusive=True, group="turn")
    async def execute_command(self, command: str) -> None:
        if self.busy and command not in {"/stop", "/approvals"}:
            self.notify("任务运行中；当前仅建议使用 /stop 或处理审批。", severity="warning")
            return
        self._ephemeral_messages = [{"role": "user", "content": command}]
        await self._render_transcript()
        try:
            response = await self.runtime.command(self.current_session_id, command)
            switch_to = response.get("switchToSessionId")
            if switch_to:
                self.current_session_id = switch_to
                self._ephemeral_messages.clear()
            else:
                self._ephemeral_messages.append(
                    {"role": "assistant", "content": response.get("result", "")}
                )
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            self._ephemeral_messages.append(
                {"role": "assistant", "content": f"**命令失败**\n\n{detail}"}
            )
            self.notify(str(detail), severity="error")
        await self.refresh_all(keep_ephemeral=True)
        self.query_one("#composer", Composer).focus()

    @work(exclusive=True, group="session-refresh")
    async def refresh_session(self) -> None:
        await self.refresh_all()
        self.query_one("#composer", Composer).focus()

    @on(Composer.Changed, "#composer")
    def composer_changed(self, event: Composer.Changed) -> None:
        value = event.text_area.text.strip()
        hints = self.query_one("#command-hints", Static)
        if not value.startswith("/") or " " in value:
            self._command_matches = []
            event.text_area.completion = ""
            hints.display = False
            return
        self._command_matches = [
            (command, description)
            for command, description in COMMANDS
            if command.startswith(value)
        ]
        self._command_index = 0
        if not self._command_matches:
            event.text_area.completion = ""
            hints.display = False
            return
        event.text_area.completion = self._command_matches[0][0]
        self._render_command_hints()
        hints.display = True

    @on(Composer.PaletteMove)
    def move_command_palette(self, event: Composer.PaletteMove) -> None:
        if not self._command_matches:
            return
        self._command_index = (
            self._command_index + event.direction
        ) % len(self._command_matches)
        self.query_one("#composer", Composer).completion = self._command_matches[
            self._command_index
        ][0]
        self._render_command_hints()

    def _render_command_hints(self) -> None:
        hints = self.query_one("#command-hints", Static)
        renderable = Text()
        total = len(self._command_matches)
        start = max(
            0,
            min(
                self._command_index - COMMAND_WINDOW_SIZE // 2,
                total - COMMAND_WINDOW_SIZE,
            ),
        )
        end = min(total, start + COMMAND_WINDOW_SIZE)
        if start:
            renderable.append(f"  ↑ {start} 条命令\n", style="#6f6872")
        for offset, (command, description) in enumerate(
            self._command_matches[start:end],
            start=start,
        ):
            if offset > start:
                renderable.append("\n")
            selected = offset == self._command_index
            renderable.append("› " if selected else "  ", style="bold #f04f65")
            renderable.append(
                f"{command:<13}",
                style="bold #f4c56a" if selected else "#d65063",
            )
            renderable.append(
                description,
                style="#ded6df" if selected else "#8f8793",
            )
        if end < total:
            renderable.append(f"\n  ↓ {total - end} 条命令", style="#6f6872")
        hints.update(renderable)

    @on(Button.Pressed, "#open-cron")
    def quick_cron(self) -> None:
        self.action_cron()

    @on(Button.Pressed)
    def approval_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("approve-"):
            approval_id = button_id.removeprefix("approve-")
            if self.runtime.approve(approval_id):
                self.notify("操作已批准。", severity="information")
                self._refresh_approvals()
        elif button_id.startswith("reject-"):
            approval_id = button_id.removeprefix("reject-")
            if self.runtime.reject(approval_id, "用户在 TUI 中拒绝"):
                self.notify("操作已拒绝。", severity="warning")
                self._refresh_approvals()

    def action_sessions(self) -> None:
        self.push_screen(
            SessionBoard(self.runtime, self.current_session_id),
            self._handle_board_result,
        )

    def action_cron(self) -> None:
        self.push_screen(CronBoard(self.runtime), self._handle_board_result)

    def action_command_panel(self) -> None:
        self.push_screen(CommandPanel(), self._handle_command_panel_result)

    def action_refresh(self) -> None:
        self.refresh_session()
        self.notify("界面状态已刷新。")

    def action_stop_turn(self) -> None:
        was_busy = self.busy
        self.runtime.stop(self.current_session_id)
        self.clear_notifications()
        self.notify(
            "已请求停止当前任务。" if was_busy else "当前没有运行中的任务。",
            severity="warning" if was_busy else "information",
            markup=False,
        )

    def action_escape(self) -> None:
        self.query_one("#composer", Composer).focus()

    def _handle_board_result(self, result: str | None) -> None:
        if not result:
            return
        if result.startswith("::cron-run "):
            self.run_cron_now(result.split(maxsplit=1)[1])
        else:
            self.execute_command(result)

    def _handle_command_panel_result(self, command: str | None) -> None:
        if not command:
            return
        composer = self.query_one("#composer", Composer)
        composer.text = command + " "
        composer.move_cursor((0, len(composer.text)))
        composer.focus()

    @work(exclusive=True, group="cron-run")
    async def run_cron_now(self, job_id: str) -> None:
        ok = await self.runtime.trigger_cron(job_id)
        self.notify(
            "Cron 作业已加入立即运行队列。" if ok else "Cron 作业未能运行。",
            severity="information" if ok else "error",
        )
        self._refresh_status()
