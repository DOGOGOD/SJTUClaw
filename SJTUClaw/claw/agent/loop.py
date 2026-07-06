"""Agent loop — the single, unified entry point for all claw interactions.

``run_agent_turn(session_id, user_message, ...)`` is the **only** place
where the LLM is called as part of a user-facing conversation. Every
entry point (CLI, Gateway, Scheduler, …) must route through this
function and must never call ``LLMClient`` directly.

Loop flow::

    user message
      -> append to session
      -> loop:
           buildContext (messages + tool definitions)
           -> callLLM
           -> if final:
                save assistant message -> return
           -> if tool_call(s):
                execute each tool (max 5 / round)
                save tool results as ``tool``-role messages
                -> continue loop

There is **no** hard iteration cap on the outer loop. The single-round
cap of 5 tool calls is enforced by ``parse_agent_response``.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.llm.protocol import AgentResponse
from claw.session.store import SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry, ToolResult


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent_turn(
    session_id: str,
    user_message: str,
    *,
    session_store: SessionStore,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
    llm_client: LLMClient,
) -> str:
    """Run a single agent turn: user message in, assistant reply out.

    This is the **only** call-site that talks to the LLM during a normal
    conversation. It:

    1. Writes the user message into *session_id*.
    2. Enters an inner think→act→observe loop:
       - Builds the full context (stable + conversation).
       - Passes tool definitions natively (API ``tools`` param) and falls
         back to JSON-protocol parsing if the model text-replies instead.
       - If the model responds ``final``: saves the assistant message and
         returns the reply text.
       - If the model requests ``tool_call(s)``: executes every requested
         tool via ``ToolRegistry`` (max 5 per round), saves each result
         as a ``tool``-role message, prints a trace line, and loops.
    3. Tool failures are fed back as observation messages — the loop
       never crashes because of a tool error.

    Returns:
        The assistant's final reply text.

    Raises:
        LLMError: on unrecoverable API failures.
        SessionStoreError: if session persistence fails at critical
            points (the error is also raised so the caller can surface
            it; in-memory state is kept up-to-date regardless).
    """
    session = session_store.get(session_id)

    # -- 1. Append user message ----------------------------------------------
    session.append_message("user", user_message)
    _save_safe(session_store, session)

    # -- 2. Think → Act → Observe loop ---------------------------------------
    turn_count = 0
    while True:
        turn_count += 1

        # 2a. Build context
        messages = context_builder.build_messages(
            session,
            tool_registry=tool_registry,
            include_tool_instructions=True,  # JSON fallback path
        )
        tool_defs = context_builder.get_tool_definitions(tool_registry)

        # 2b. Call LLM
        response = llm_client.chat_with_tools(messages, tool_defs)

        # 2c. Final answer — done
        if response.is_final and response.final is not None:
            final_text = response.final
            session.append_message("assistant", final_text)
            _save_safe(session_store, session)
            return final_text

        # 2d. Tool call(s) — execute and feed back
        if response.is_tool_call:
            _execute_and_record_tool_calls(
                response, session, session_store, tool_registry
            )
            # Loop continues — model gets another call with the tool
            # results now in the session messages.
            continue

        # 2e. Neither final nor tool_call (should not happen, but be safe)
        # Treat empty tool_calls list as a final with empty content
        empty_reply = ""
        session.append_message("assistant", empty_reply)
        _save_safe(session_store, session)
        return empty_reply


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _execute_and_record_tool_calls(
    response: AgentResponse,
    session,
    session_store: SessionStore,
    tool_registry: ToolRegistry,
) -> None:
    """Execute every tool call in *response* and record results.

    Each tool result is appended to *session* as a ``tool``-role message.
    Traces are printed to stdout for debugging / verification.
    """
    for tc in response.tool_calls:
        # Print trace
        args_json = json.dumps(tc.args, ensure_ascii=False)
        print(f"[tool_call] {tc.name} {args_json}")

        # Execute
        result = tool_registry.execute_by_name(tc.name, tc.args)

        # Format result for the session message
        if result.ok:
            result_content = result.content or "(空结果)"
            print(f"[tool_result] {tc.name}: 成功")
        else:
            result_content = f"错误: {result.error}"
            print(f"[tool_result] {tc.name}: 失败 — {result.error}")

        # Write tool message into session
        tool_msg_content = json.dumps(
            {"tool": tc.name, "ok": result.ok, "result": result_content},
            ensure_ascii=False,
        )
        session.append_message("tool", tool_msg_content)

    _save_safe(session_store, session)


def _save_safe(session_store: SessionStore, session) -> None:
    """Persist *session*, printing a warning but not crashing on failure."""
    try:
        session_store.save(session)
    except SessionStoreError as exc:
        print(f"[警告] session 保存失败: {exc}")
