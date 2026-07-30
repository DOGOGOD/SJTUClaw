"""Reusable widgets for the SJTUClaw terminal interface."""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Label, Static, TextArea


class Composer(TextArea):
    """Multi-line composer where Enter submits and Shift+Enter inserts a line."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class PaletteMove(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    completion = ""

    def on_key(self, event: events.Key) -> None:
        command_head = self.text.strip()
        palette_open = (
            command_head.startswith("/")
            and " " not in command_head
            and "\n" not in self.text
        )
        key_parts = set(event.key.split("+"))
        shift_enter = "shift" in key_parts and (
            "enter" in key_parts
            or "newline" in key_parts
            or event.character in {"\r", "\n"}
            or bool({"j", "m"} & key_parts and "ctrl" in key_parts)
        )
        if shift_enter:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if palette_open and event.key in {"up", "down"}:
            event.prevent_default()
            event.stop()
            self.post_message(self.PaletteMove(-1 if event.key == "up" else 1))
            return
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
        classes = f"message-card role-{role}"
        super().__init__(id=f"message-{index}", classes=classes)

    def compose(self) -> ComposeResult:
        role_label = {
            "user": "YOU",
            "assistant": "SJTUCLAW",
            "tool": f"TOOL · {self.message_data.get('name', 'RESULT')}",
            "system": "SYSTEM",
        }.get(self.role, self.role.upper())
        yield Label(role_label, classes="message-role", markup=False)
        content = str(self.message_data.get("content") or "")
        if self.role in {"assistant", "system"}:
            yield Static(RichMarkdown(content or " "), classes="message-body")
        else:
            yield Static(content or " ", classes="message-body", markup=False)


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
        approval_id = self.approval["approvalId"]
        yield Label("需要你的确认", classes="approval-title")
        detail = (
            f"{self.approval['toolName']}\n"
            f"{self.approval.get('toolArgs', {})}"
        )
        if len(detail) > 1200:
            detail = detail[:1200].rstrip() + "\n…"
        yield Static(
            detail,
            classes="approval-detail",
            markup=False,
        )
        with Horizontal(classes="approval-actions"):
            yield Button("批准", id=f"approve-{approval_id}", variant="success")
            yield Button("拒绝", id=f"reject-{approval_id}", variant="error")
