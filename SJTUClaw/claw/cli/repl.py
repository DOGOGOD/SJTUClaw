"""Interactive terminal REPL for multi-turn conversation with claw.

This module owns terminal input/output only:
    - Session storage & persistence -> `claw.session`
    - `/session`, `/memory`, `/compact` commands -> `claw.cli.commands`
    - Assembling the LLM `messages` array -> `claw.context.builder`
    - Agent turn orchestration -> `claw.agent.loop`
    - Tool execution -> `claw.tools`
    - Auto-compaction after each turn -> `claw.context.compaction`
"""

from __future__ import annotations

from claw.agent.loop import run_agent_turn
from claw.cli.commands import RuntimeState, handle_command, is_command
from claw.context.builder import ContextBuilder
from claw.context.compaction import (
    CompactionError,
    CompactionOutcome,
    compact_and_persist,
    needs_compaction,
)
from claw.llm.client import LLMClient, LLMError
from claw.memory.store import MemoryStore
from claw.session.models import Session
from claw.session.store import SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry

EXIT_COMMANDS = {"/exit"}
WELCOME_MESSAGE = "claw started. Type /exit to quit."


def run_repl(
    client: LLMClient,
    session_store: SessionStore,
    memory_store: MemoryStore,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
) -> None:
    """Run the interactive multi-turn conversation loop."""
    initial_session = session_store.ensure_default_session()
    state = RuntimeState(
        session_store=session_store,
        memory_store=memory_store,
        llm_client=client,
        current_session_id=initial_session.session_id,
    )

    print(WELCOME_MESSAGE)
    _print_load_warnings(session_store, memory_store)
    print(f"Current session: {state.current_session_id}")
    print()

    while True:
        user_input = _read_user_input()
        if user_input is None:
            _print_goodbye()
            return

        if not user_input:
            continue

        if user_input in EXIT_COMMANDS:
            _print_goodbye()
            return

        if is_command(user_input):
            print(handle_command(user_input, state))
            print()
            continue

        _handle_chat_turn(
            user_input, state, client, context_builder, tool_registry
        )


def _handle_chat_turn(
    user_input: str,
    state: RuntimeState,
    client: LLMClient,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
) -> None:
    """Process one user message through the unified agent loop."""
    try:
        reply = run_agent_turn(
            state.current_session_id,
            user_input,
            session_store=state.session_store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
        )
    except LLMError as exc:
        _print_error(exc)
        return

    if reply:
        _print_assistant_reply(reply)

    # -- Auto-compaction check -----------------------------------------------
    _maybe_auto_compact(state)
    print()


def _maybe_auto_compact(state: RuntimeState) -> None:
    """Check and run compaction after a turn, per Step 4's agent loop rule.

    Only ever touches ``session.summary`` / ``session.messages``.
    """
    session = state.session_store.get(state.current_session_id)
    if not needs_compaction(session):
        return

    try:
        outcome = compact_and_persist(
            session, state.session_store, state.llm_client
        )
    except CompactionError as exc:
        _print_error(exc)
        return

    _print_compaction_outcome(session.session_id, outcome)


def _print_compaction_outcome(session_id: str, outcome: CompactionOutcome) -> None:
    result = outcome.result
    print(
        f"[system] compact session {session_id}: "
        f"old_messages={result.old_message_count}, recent_messages={result.recent_message_count}"
    )
    print("[system] summary:")
    print(result.summary)
    if outcome.save_error:
        print(f"[system] warning: 压缩结果保存可能未成功: {outcome.save_error}")


def _print_load_warnings(
    session_store: SessionStore, memory_store: MemoryStore
) -> None:
    for warning in session_store.load_warnings:
        print(f"[警告] {warning}")
    if memory_store.load_warning:
        print(f"[警告] {memory_store.load_warning}")


def _read_user_input() -> str | None:
    """Read one line of user input.

    Returns:
        The stripped input string, or None if the user asked to quit
        via EOF (Ctrl+D) or interrupted the program (Ctrl+C).
    """
    try:
        return input("User> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _print_assistant_reply(reply: str) -> None:
    print(f"Assistant> {reply}")


def _print_error(error: Exception) -> None:
    print(f"[错误] {error}")


def _print_goodbye() -> None:
    print("bye.")
