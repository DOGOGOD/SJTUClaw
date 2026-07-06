"""Context builder: turns storage structures into LLM input structure.

This is the *only* place that assembles the `messages` array sent to
the LLM. CLI code and the LLM client must never build this array
themselves; they must go through ``ContextBuilder.build_messages()``.

Assembly order (stable context first, conversation context last)::

    system prompt -> soul -> memory -> tool descriptions[*] ->
    session summary -> recent session messages

[*] Tool descriptions are embedded in the system messages only for the
JSON-protocol fallback path. When native function calling is available,
tool definitions travel via the API ``tools`` parameter and do not need
to be duplicated in the message content.
"""

from __future__ import annotations

from typing import Any

from claw.memory.store import MemoryStore
from claw.session.models import Session


class ContextBuilder:
    """Assembles system prompt, soul, memory, tool info and session history."""

    def __init__(self, system_prompt: str, soul: str, memory_store: MemoryStore):
        self._system_prompt = system_prompt
        self._soul = soul
        self._memory_store = memory_store

    # -- public API ----------------------------------------------------------

    def build_messages(
        self,
        session: Session,
        tool_registry=None,
        include_tool_instructions: bool = True,
    ) -> list[dict[str, str]]:
        """Build the full ``messages`` array to send to the LLM for *session*.

        Args:
            session: the current session whose history to include.
            tool_registry: optional ``ToolRegistry``. When provided, its
                ``list_compact_definitions()`` are embedded in a system
                message (only relevant for the JSON-protocol fallback).
            include_tool_instructions: when False, tool definitions and
                protocol instructions are omitted from the context (e.g.
                when the caller passes them via the API ``tools`` param).
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "system", "content": self._soul},
        ]

        memory_block = self._build_memory_block()
        if memory_block is not None:
            messages.append({"role": "system", "content": memory_block})

        # Tool definitions + protocol (JSON fallback path only)
        if include_tool_instructions and tool_registry is not None:
            tool_defs = tool_registry.list_compact_definitions()
            if tool_defs:
                from claw.llm.protocol import build_protocol_instructions

                messages.append(
                    {"role": "system", "content": build_protocol_instructions(tool_defs)}
                )

        summary_block = self._build_summary_block(session)
        if summary_block is not None:
            messages.append({"role": "system", "content": summary_block})

        messages.extend(message.to_dict() for message in session.messages)
        return messages

    def get_tool_definitions(self, tool_registry=None) -> list[dict[str, Any]]:
        """Return OpenAI-format tool definitions for the API ``tools`` param.

        Returns an empty list if *tool_registry* is None.
        """
        if tool_registry is None:
            return []
        return tool_registry.list_definitions()

    def update_system_prompt(self, content: str) -> None:
        """Hot-reload the system prompt without restarting the server."""
        self._system_prompt = content

    def update_soul(self, content: str) -> None:
        """Hot-reload the soul without restarting the server."""
        self._soul = content

    # -- internal ------------------------------------------------------------

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
