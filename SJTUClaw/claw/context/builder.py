"""Context builder: turns storage structures into LLM input structure.

This is the *only* place that assembles the `messages` array sent to
the LLM. CLI code and the LLM client must never build this array
themselves; they must go through `ContextBuilder.build_messages()`.

Assembly order (stable context first, conversation context last):
    system prompt -> soul -> memory -> session summary -> recent session messages

Note: `session summary` here is `session.summary`, produced by
`claw.context.compaction`. It belongs to a single session (unlike
memory, which is cross-session) and is inserted right before that
session's own (recent, uncompacted) messages.
"""

from __future__ import annotations

from claw.memory.store import MemoryStore
from claw.session.models import Session


class ContextBuilder:
    """Assembles system prompt, soul, memory and session history."""

    def __init__(self, system_prompt: str, soul: str, memory_store: MemoryStore):
        self._system_prompt = system_prompt
        self._soul = soul
        self._memory_store = memory_store

    def build_messages(self, session: Session) -> list[dict[str, str]]:
        """Build the full `messages` array to send to the LLM for `session`."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "system", "content": self._soul},
        ]

        memory_block = self._build_memory_block()
        if memory_block is not None:
            messages.append({"role": "system", "content": memory_block})

        summary_block = self._build_summary_block(session)
        if summary_block is not None:
            messages.append({"role": "system", "content": summary_block})

        messages.extend(message.to_dict() for message in session.messages)
        return messages

    def _build_memory_block(self) -> str | None:
        entries = self._memory_store.list()
        if not entries:
            return None
        lines = "\n".join(f"- {entry.content}" for entry in entries)
        return (
            "以下是关于用户的长期记忆（memory），跨 session 长期有效，"
            "请在回答时参考：\n" + lines
        )

    @staticmethod
    def _build_summary_block(session: Session) -> str | None:
        summary = session.summary.strip()
        if not summary:
            return None
        return (
            "以下是当前 session 较早对话的摘要（session summary），"
            "这部分历史已经被压缩、不再以原始消息形式保留，"
            "请在回答时结合它一起考虑：\n" + summary
        )
