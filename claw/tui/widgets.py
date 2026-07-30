"""Reusable widgets for the SJTUClaw terminal interface."""

from __future__ import annotations

import json
from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Label, Static, TextArea

APPROVAL_DETAIL_LIMIT = 20_000


class Composer(TextArea):
    """Multi-line composer where Enter submits and Ctrl+N inserts a line."""

    HISTORY_LIMIT = 200

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index = 0
        self._history_draft = ""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class PaletteMove(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    completion = ""

    def set_history(self, entries: list[str]) -> None:
        """Replace the current Session's input history."""
        self._history = [
            entry for entry in entries if entry.strip()
        ][-self.HISTORY_LIMIT :]
        self._history_index = len(self._history)
        self._history_draft = self.text

    def _move_history(self, direction: int) -> bool:
        if not self._history:
            return False
        history_end = len(self._history)
        if direction < 0:
            if self._history_index == history_end:
                self._history_draft = self.text
            if self._history_index == 0:
                return True
            self._history_index -= 1
            value = self._history[self._history_index]
        else:
            if self._history_index == history_end:
                return True
            self._history_index += 1
            value = (
                self._history_draft
                if self._history_index == history_end
                else self._history[self._history_index]
            )
        self.text = value
        self.move_cursor(self.document.end)
        return True

    def _reset_history_navigation(self) -> None:
        self._history_index = len(self._history)

    def on_key(self, event: events.Key) -> None:
        command_head = self.text.strip()
        palette_open = (
            command_head.startswith("/")
            and " " not in command_head
            and "\n" not in self.text
        )
        if event.key == "ctrl+n":
            self._reset_history_navigation()
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "ctrl+m":
            event.prevent_default()
            event.stop()
            return
        if palette_open and event.key in {"up", "down"}:
            event.prevent_default()
            event.stop()
            self.post_message(self.PaletteMove(-1 if event.key == "up" else 1))
            return
        history_direction = (
            -1
            if event.key == "up" and self.cursor_at_first_line
            else 1
            if event.key == "down" and self.cursor_at_last_line
            else 0
        )
        if history_direction and self._move_history(history_direction):
            event.prevent_default()
            event.stop()
            return
        if event.key not in {"up", "down"}:
            self._reset_history_navigation()
        if palette_open and event.key == "tab" and self.completion:
            event.prevent_default()
            event.stop()
            self.text = self.completion + " "
            self.move_cursor((0, len(self.text)))
            return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            value = self.text.rstrip()
            if (
                palette_open
                and self.completion
                and value.strip() != self.completion
            ):
                self.text = self.completion + " "
                self.move_cursor((0, len(self.text)))
                return
            if value.strip():
                self.post_message(self.Submitted(value))


class BrandHeader(Static):
    def render(self) -> Text:
        text = Text()
        text.append(" SJTU", style="bold #f3e8d6")
        text.append("CLAW ", style="bold #e34b5f")
        text.append("  TERMINAL AGENT", style="#827b88")
        return text


class MessageCard(Static):
    """Rich chat transcript card."""

    def __init__(self, message: dict[str, Any], index: int) -> None:
        role = str(message.get("role", "assistant"))
        self.role = role
        self.message_data = message
        tool_calls = message.get("tool_calls")
        self.tool_calls = tool_calls if isinstance(tool_calls, list) else []
        classes = (
            "message-card role-tool-request"
            if self.tool_calls
            else f"message-card role-{role}"
        )
        super().__init__(id=f"message-{index}", classes=classes)

    def compose(self) -> ComposeResult:
        if self.tool_calls:
            names = ", ".join(self._tool_call_name(call) for call in self.tool_calls)
            role_label = f"TOOL REQUEST · {names}"
        else:
            role_label = {
                "user": "YOU",
                "assistant": "SJTUCLAW",
                "tool": f"TOOL · {self.message_data.get('name', 'RESULT')}",
                "system": "SYSTEM",
            }.get(self.role, self.role.upper())
        yield Label(role_label, classes="message-role", markup=False)
        content = str(self.message_data.get("content") or "")
        if content and self.role in {"assistant", "system"}:
            yield Static(RichMarkdown(content or " "), classes="message-body")
        elif content:
            yield Static(content or " ", classes="message-body", markup=False)
        if self.tool_calls:
            yield Static(
                self._format_tool_calls(self.tool_calls),
                classes="message-body tool-call-body",
                markup=False,
            )
        elif not content:
            yield Static(" ", classes="message-body", markup=False)

    @staticmethod
    def _tool_call_name(call: Any) -> str:
        if not isinstance(call, dict):
            return "tool"
        function = call.get("function")
        source = function if isinstance(function, dict) else call
        return str(source.get("name") or "tool")

    @classmethod
    def _format_tool_calls(cls, tool_calls: list[Any]) -> str:
        sections: list[str] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                sections.append(str(call))
                continue
            function = call.get("function")
            source = function if isinstance(function, dict) else call
            arguments = source.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            argument_text = (
                json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
                if not isinstance(arguments, str)
                else arguments
            )
            sections.append(f"{cls._tool_call_name(call)}\n{argument_text}")
        summary = "\n\n".join(sections)
        return summary if len(summary) <= 2000 else summary[:2000].rstrip() + "\n…"


class WelcomePanel(Static):
    def __init__(self, quote: str, author: str) -> None:
        self.quote = quote
        self.author = author
        super().__init__(id="welcome-panel")

    def compose(self) -> ComposeResult:
        yield Static("◢", classes="welcome-glyph")
        yield Static("从一个问题开始。", classes="welcome-title")
        yield Static(
            "直接描述任务，或输入  /  查看全部命令。"
            "\nCtrl+S 管理会话，Ctrl+J 查看 Cron；运行状态与审批保持可见。",
            classes="welcome-copy",
        )
        yield Static(f"“{self.quote}”\n  — {self.author}", classes="quote")


class ApprovalCard(Static):
    """Inline approval surface that keeps risky actions explicit."""

    def __init__(self, approval: dict[str, Any]) -> None:
        self.approval = approval
        super().__init__(classes="approval-card")

    def compose(self) -> ComposeResult:
        approval_id = str(self.approval["approvalId"])
        yield Label("需要你的确认", classes="approval-title")
        detail = (
            f"{self.approval['toolName']}\n"
            f"{self.approval.get('toolArgs', {})}"
        )
        if len(detail) > APPROVAL_DETAIL_LIMIT:
            detail = detail[:APPROVAL_DETAIL_LIMIT].rstrip() + "\n…"
        yield VerticalScroll(
            Static(detail, markup=False),
            classes="approval-detail",
        )
        with Horizontal(classes="approval-actions"):
            yield Button(
                "批准",
                name=approval_id,
                classes="approval-approve",
                variant="success",
                compact=True,
            )
            yield Button(
                "拒绝",
                name=approval_id,
                classes="approval-reject",
                variant="error",
                compact=True,
            )
