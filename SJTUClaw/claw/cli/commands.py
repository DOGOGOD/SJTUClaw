"""Parsing and dispatch for claw's internal CLI commands.

`/session ...`, `/memory ...` and `/compact` commands are intercepted
here and are never forwarded to the LLM as ordinary chat messages.
This module knows nothing about terminal I/O beyond returning plain
text for the caller to print; `/compact` is the only command that also
talks to the LLM (via `claw.context.compaction`), to generate a new
session summary.
"""

from __future__ import annotations

from dataclasses import dataclass

from claw.context.compaction import (
    KEEP_RECENT_MESSAGES,
    CompactionError,
    compact_and_persist,
)
from claw.llm.client import LLMClient
from claw.memory.store import MemoryStore, MemoryStoreError
from claw.session.store import SessionNotFoundError, SessionStore, SessionStoreError

_COMMAND_PREFIXES = ("/session", "/memory", "/compact")


@dataclass
class RuntimeState:
    """Mutable CLI-level state shared across command handlers."""

    session_store: SessionStore
    memory_store: MemoryStore
    llm_client: LLMClient
    current_session_id: str


def is_command(user_input: str) -> bool:
    """Return True if `user_input` is a `/session` or `/memory` command."""
    return any(
        user_input == prefix or user_input.startswith(prefix + " ")
        for prefix in _COMMAND_PREFIXES
    )


def handle_command(user_input: str, state: RuntimeState) -> str:
    """Handle a command and return the text to print.

    Assumes `is_command(user_input)` is True.
    """
    root, *args = user_input.split()

    if root == "/session":
        return _handle_session_command(args, state)
    if root == "/memory":
        return _handle_memory_command(user_input, args, state)
    if root == "/compact":
        return _handle_compact_command(state)
    return f"未知命令: {root}"


# -- /session ---------------------------------------------------------------


def _handle_session_command(args: list[str], state: RuntimeState) -> str:
    if not args:
        return "用法: /session <new|list|switch|rename|delete> ..."

    sub, rest = args[0], args[1:]

    if sub == "new":
        session = state.session_store.create_session()
        state.current_session_id = session.session_id
        return f"Created session: {session.session_id}"

    if sub == "list":
        return _format_session_list(state)

    if sub == "switch":
        if not rest:
            return "用法: /session switch <sessionId>"
        return _switch_session(rest[0], state)

    if sub == "rename":
        if len(rest) < 2:
            return "用法: /session rename <sessionId> <title>"
        session_id, title = rest[0], " ".join(rest[1:])
        try:
            state.session_store.rename(session_id, title)
        except (SessionNotFoundError, SessionStoreError) as exc:
            return f"[错误] {exc}"
        return f"Renamed session {session_id} to: {title}"

    if sub == "delete":
        if not rest:
            return "用法: /session delete <sessionId>"
        return _delete_session(rest[0], state)

    return f"未知 /session 子命令: {sub}"


def _switch_session(session_id: str, state: RuntimeState) -> str:
    try:
        state.session_store.get(session_id)
    except SessionNotFoundError as exc:
        return f"[错误] {exc}"
    state.current_session_id = session_id
    return f"Switched to session: {session_id}"


def _delete_session(session_id: str, state: RuntimeState) -> str:
    try:
        state.session_store.delete(session_id)
    except (SessionNotFoundError, SessionStoreError) as exc:
        return f"[错误] {exc}"

    if state.current_session_id != session_id:
        return f"Deleted session: {session_id}"

    summaries = state.session_store.list_summaries()
    if summaries:
        fallback_id = summaries[0].session_id
    else:
        fallback_id = state.session_store.ensure_default_session().session_id
    state.current_session_id = fallback_id
    return (
        f"Deleted session: {session_id}\n"
        f"当前 session 已被删除，已自动切换到: {fallback_id}"
    )


def _format_session_list(state: RuntimeState) -> str:
    summaries = state.session_store.list_summaries()
    if not summaries:
        return "Sessions: (empty)"

    lines = ["Sessions:"]
    for summary in summaries:
        marker = "*" if summary.session_id == state.current_session_id else " "
        lines.append(
            f"{marker} {summary.session_id}\t{summary.title}\t"
            f"messages={summary.message_count}\tupdated={summary.updated_at}"
        )
    return "\n".join(lines)


# -- /memory ------------------------------------------------------------


def _handle_memory_command(raw_input: str, args: list[str], state: RuntimeState) -> str:
    if not args:
        return "用法: /memory <add|list|delete> ..."

    sub = args[0]

    if sub == "add":
        return _add_memory(raw_input, state)

    if sub == "list":
        return _format_memory_list(state)

    if sub == "delete":
        if len(args) < 2:
            return "用法: /memory delete <memoryId>"
        return _delete_memory(args[1], state)

    return f"未知 /memory 子命令: {sub}"


def _add_memory(raw_input: str, state: RuntimeState) -> str:
    prefix = "/memory add "
    if not raw_input.startswith(prefix):
        return "用法: /memory add <content>"
    content = raw_input[len(prefix) :].strip()
    try:
        entry = state.memory_store.add(content)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"
    return f"Added memory: {entry.memory_id}"


def _format_memory_list(state: RuntimeState) -> str:
    entries = state.memory_store.list()
    if not entries:
        return "Memory: (empty)"
    lines = ["Memory:"]
    for entry in entries:
        lines.append(f"  {entry.memory_id}\t{entry.content}")
    return "\n".join(lines)


def _delete_memory(memory_id: str, state: RuntimeState) -> str:
    try:
        state.memory_store.delete(memory_id)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"
    return f"Deleted memory: {memory_id}"


# -- /compact -----------------------------------------------------------


def _handle_compact_command(state: RuntimeState) -> str:
    session = state.session_store.get(state.current_session_id)

    if len(session.messages) <= KEEP_RECENT_MESSAGES:
        return (
            f"当前 session 只有 {len(session.messages)} 条消息，"
            f"不超过保留窗口（{KEEP_RECENT_MESSAGES}），无需压缩。"
        )

    try:
        outcome = compact_and_persist(session, state.session_store, state.llm_client)
    except CompactionError as exc:
        return f"[错误] {exc}"

    result = outcome.result
    lines = [
        f"Compacted session {session.session_id}.",
        f"Old messages: {result.old_message_count}",
        f"Recent messages: {result.recent_message_count}",
        "Summary updated: yes",
        "Summary:",
        result.summary,
    ]
    if outcome.save_error:
        lines.append(f"[警告] 压缩结果保存可能未成功: {outcome.save_error}")
    return "\n".join(lines)
