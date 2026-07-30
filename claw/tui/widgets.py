"""Reusable widgets for the SJTUClaw terminal interface."""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
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
        palette_open = command_head.startswith("/") and " " not in command_head
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
            value = self.text.strip()
            if (
                palette_open
                and self.completion
                and value != self.completion
            ):
                self.text = self.completion + " "
                self.move_cursor((0, len(self.text)))
                return
            if value:
                self.post_message(self.Submitted(value))


class BrandHeader(Static):
    def render(self) -> Text:
        text = Text()
        text.append(" SJTU", style="bold #f3e8d6")
        text.append("CLAW ", style="bold #e34b5f")
        text.append("  TERMINAL AGENT", style="#827b88")
        return text


class ModePill(Static):
    """Small status pill."""


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
        yield Label(role_label, classes="message-role")
        content = str(self.message_data.get("content") or "")
        if self.role in {"assistant", "system"}:
            yield Static(RichMarkdown(content or " "), classes="message-body")
        else:
            yield Static(content or " ", classes="message-body")


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
            "\n会话、Cron、审批与运行环境都在两侧看板中保持可见。",
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
        yield Static(
            f"{self.approval['toolName']}\n{self.approval.get('toolArgs', {})}",
            classes="approval-detail",
        )
        with Horizontal(classes="approval-actions"):
            yield Button("批准  A", id=f"approve-{approval_id}", variant="success")
            yield Button("拒绝  R", id=f"reject-{approval_id}", variant="error")


class EmptyRow(Vertical):
    def __init__(self, text: str) -> None:
        super().__init__(Static(text, classes="muted"))
