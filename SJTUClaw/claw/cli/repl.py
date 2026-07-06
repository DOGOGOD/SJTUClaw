"""Interactive terminal REPL for multi-turn conversation with claw.

This module owns terminal input/output only:
    - Session storage & persistence -> `claw.session`
    - `/session`, `/memory`, `/compact` commands -> `claw.cli.commands`
    - Assembling the LLM `messages` array -> `claw.context.builder`
    - Talking to the LLM -> `claw.llm.client`
    - Auto-compaction after each turn -> `claw.context.compaction`
"""

from __future__ import annotations

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

EXIT_COMMANDS = {"/exit"}
WELCOME_MESSAGE = "claw started. Type /exit to quit."


def run_repl(
    client: LLMClient,
    session_store: SessionStore,
    memory_store: MemoryStore,
    context_builder: ContextBuilder,
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

        _handle_chat_turn(user_input, state, client, context_builder)


def _handle_chat_turn(
    user_input: str,
    state: RuntimeState,
    client: LLMClient,
    context_builder: ContextBuilder,
) -> None:
    session = state.session_store.get(state.current_session_id)
    session.append_message("user", user_input)
    _save_session_quietly(state.session_store, session)

    try:
        messages = context_builder.build_messages(session)
        reply = client.chat(messages)
    except LLMError as exc:
        _print_error(exc)
        return

    session.append_message("assistant", reply)
    _save_session_quietly(state.session_store, session)
    _print_assistant_reply(reply)

    _maybe_auto_compact(session, state, client)


def _maybe_auto_compact(
    session: Session, state: RuntimeState, client: LLMClient
) -> None:
    """Check and run compaction after a turn, per Step 4's agent loop rule.

    Only ever touches `session.summary`/`session.messages`; failures
    here never delete the assistant reply that was just appended.
    """
    if not needs_compaction(session):
        return

    try:
        outcome = compact_and_persist(session, state.session_store, client)
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
    print()


def _save_session_quietly(session_store: SessionStore, session) -> None:
    try:
        session_store.save(session)
    except SessionStoreError as exc:
        _print_error(exc)


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
    print()


def _print_error(error: Exception) -> None:
    print(f"[错误] {error}")
    print()


def _print_goodbye() -> None:
    print("bye.")
