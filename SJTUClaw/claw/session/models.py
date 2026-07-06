"""Session and message data models.

These classes describe the *storage* structure only. Turning a
`Session` into the `messages` array sent to the LLM is the
responsibility of `claw.context.builder.ContextBuilder`, not of this
module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Message:
    """A single chat message (`role` + `content`)."""

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        try:
            role = data["role"]
            content = data["content"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"消息数据格式错误，缺少字段: {data!r}") from exc
        if not isinstance(role, str) or not isinstance(content, str):
            raise ValueError(f"消息数据格式错误，字段类型不正确: {data!r}")
        return cls(role=role, content=content)


@dataclass
class Session:
    """A single conversation session with its own isolated history.

    `summary` holds the compaction summary for older messages that have
    already been compressed out of `messages` (see
    `claw.context.compaction`). It belongs to this session only and is
    never shared across sessions (that is what `memory` is for).
    """

    session_id: str
    title: str
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def append_message(self, role: str, content: str) -> None:
        self.messages.append(Message(role=role, content=content))
        self.touch()

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "sessionId": self.session_id,
            "title": self.title,
            "messages": [m.to_dict() for m in self.messages],
            "summary": self.summary,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        if not isinstance(data, dict):
            raise ValueError("Session 数据格式错误：顶层结构应为对象")

        try:
            session_id = data["sessionId"]
            title = data["title"]
        except KeyError as exc:
            raise ValueError(f"Session 数据格式错误，缺少字段: {exc}") from exc

        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ValueError("Session 数据格式错误：messages 字段应为数组")

        summary = data.get("summary") or ""
        if not isinstance(summary, str):
            raise ValueError("Session 数据格式错误：summary 字段应为字符串")

        created_at = data.get("createdAt") or _now_iso()
        updated_at = data.get("updatedAt") or created_at

        messages = [Message.from_dict(item) for item in raw_messages]
        return cls(
            session_id=session_id,
            title=title,
            messages=messages,
            summary=summary,
            created_at=created_at,
            updated_at=updated_at,
        )
