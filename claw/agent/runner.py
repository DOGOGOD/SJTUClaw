"""Agent runner — single-LLM-call handler with error recovery.

The runner handles ONE LLM call at a time, with:
- Context governance application
- Empty response retries (up to 2)
- Length recovery (up to 3 rounds)
- Malformed tool call recovery

Tool execution is NOT done here — the caller (loop.py) dispatches
tools through product-layer gates (approval, skill injection).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from claw.context.governance import ContextGovernor, GovernanceConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3

_EMPTY_RETRY_MESSAGE = (
    "The previous response was empty. Please provide a substantive response "
    "— either a final answer or tool calls to gather more information."
)

_LENGTH_RECOVERY_MESSAGE = (
    "Your previous response was truncated due to output length limits. "
    "Please continue exactly where you left off — do not repeat what you "
    "already said, just pick up from the cut-off point."
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AgentRunSpec:
    """Configuration for a single LLM call."""

    initial_messages: list[dict[str, Any]]
    tools: Any  # ToolRegistry
    model: str = ""
    max_tool_result_chars: int = 8000
    context_window_tokens: int | None = None
    max_output_tokens: int = 4096
    session_key: str | None = None


@dataclass
class AgentRunResult:
    """Outcome of a single LLM call."""

    final_content: str | None = None
    """Assistant reply text (None if tool calls were returned)."""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Tool call requests from the LLM (empty if final response)."""

    finish_reason: str = "completed"
    """One of: completed, tool_calls, error, empty_final_response, length."""

    assistant_message: dict[str, Any] | None = None
    """The assistant message dict for persistence."""


class AgentRunner:
    """Single-LLM-call handler with error recovery.

    Usage::

        runner = AgentRunner()
        runner.set_llm_client(client)
        result = runner.call(spec, tool_registry)
        if result.tool_calls:
            # caller dispatches tools through product gates
        else:
            # result.final_content is the assistant's reply
    """

    def __init__(self):
        self._governor = ContextGovernor()
        self._llm_client = None

    def set_llm_client(self, client) -> None:
        self._llm_client = client

    def call(self, spec: AgentRunSpec) -> AgentRunResult:
        """Make one LLM call with error recovery.

        Returns an ``AgentRunResult``:
        - If ``tool_calls`` is non-empty: the LLM requested tool calls.
          The caller should dispatch them, append results to the session,
          and call again.
        - If ``final_content`` is set: this is the assistant's final reply.
        """
        messages: list[dict[str, Any]] = list(spec.initial_messages)
        governance_config = GovernanceConfig(
            max_tool_result_chars=spec.max_tool_result_chars,
            context_window_tokens=spec.context_window_tokens,
            max_output_tokens=spec.max_output_tokens,
            inflight_start_index=len(spec.initial_messages),
            session_key=spec.session_key,
        )

        empty_retries = 0
        length_recoveries = 0

        while True:
            # 1. Apply context governance
            try:
                model_msgs = self._governor.prepare_for_model(
                    governance_config, messages, set(),
                )
            except Exception:
                model_msgs = messages

            # 2. Call LLM
            response = self._do_llm_call(spec, model_msgs)

            # 3. LLM error → return gracefully
            if response.get("finish_reason") == "error":
                err = response.get("content") or "LLM 调用失败"
                print(f"[runner] LLM 错误: {err}")
                return AgentRunResult(
                    final_content=err,
                    finish_reason="error",
                    assistant_message={"role": "assistant", "content": err},
                )

            # 4. Tool calls → return to caller for dispatch
            if response.get("tool_calls"):
                assistant_msg = self._build_assistant_message(response)
                return AgentRunResult(
                    tool_calls=response["tool_calls"],
                    finish_reason="tool_calls",
                    assistant_message=assistant_msg,
                )

            content = (response.get("content") or "").strip()

            # 4a. Empty response → retry
            if not content and response.get("finish_reason") != "error":
                empty_retries += 1
                if empty_retries < _MAX_EMPTY_RETRIES:
                    print(f"[runner] 空响应重试 ({empty_retries}/{_MAX_EMPTY_RETRIES})")
                    messages.append({"role": "user", "content": _EMPTY_RETRY_MESSAGE})
                    continue
                # Exhausted retries → try no-tools finalization
                final_content = self._no_tools_finalize(spec, messages)
                if final_content:
                    return AgentRunResult(
                        final_content=final_content,
                        finish_reason="completed",
                        assistant_message={"role": "assistant", "content": final_content},
                    )
                return AgentRunResult(
                    final_content="(no response)",
                    finish_reason="empty_final_response",
                    assistant_message={"role": "assistant", "content": "(no response)"},
                )

            # 4b. Length truncated → continue
            if response.get("finish_reason") == "length" and length_recoveries < _MAX_LENGTH_RECOVERIES:
                length_recoveries += 1
                print(f"[runner] 截断恢复 ({length_recoveries}/{_MAX_LENGTH_RECOVERIES})")
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": _LENGTH_RECOVERY_MESSAGE})
                continue

            # 4c. Success
            return AgentRunResult(
                final_content=content,
                finish_reason="completed",
                assistant_message={"role": "assistant", "content": content},
            )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _do_llm_call(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        malformed_retry: bool = False,
    ) -> dict[str, Any]:
        """Call the LLM and return a normalised response dict."""
        tool_defs = spec.tools.list_definitions() if spec.tools else []

        llm_response = self._llm_client.chat_with_tools(messages, tool_defs)

        if llm_response.is_final and llm_response.final is not None:
            return {"content": llm_response.final, "tool_calls": None, "finish_reason": "stop", "usage": {}}

        if llm_response.is_tool_call:
            tool_calls = []
            for tc in llm_response.tool_calls:
                call = {
                    "id": getattr(tc, "id", "") or f"call_{len(tool_calls)}",
                    "name": tc.name,
                    "args": tc.args,
                    "function": {"name": tc.name, "arguments": json.dumps(tc.args, ensure_ascii=False)},
                }
                if not isinstance(call["name"], str) or not call["name"]:
                    continue
                tool_calls.append(call)

            if not tool_calls and not malformed_retry:
                print("[runner] 所有 tool_calls 无效，重试...")
                retry = list(messages)
                retry.append({"role": "user", "content": "之前的工具调用无效，请使用有效工具名重试。"})
                return self._do_llm_call(spec, retry, malformed_retry=True)

            if tool_calls:
                return {"content": None, "tool_calls": tool_calls, "finish_reason": "tool_calls", "usage": {}}
            # All calls dropped, treat as empty
            return {"content": "", "tool_calls": None, "finish_reason": "stop", "usage": {}}

        return {"content": "", "tool_calls": None, "finish_reason": "stop", "usage": {}}

    def _no_tools_finalize(self, spec: AgentRunSpec, messages: list[dict[str, Any]]) -> str | None:
        """One-shot no-tools call for finalization."""
        try:
            response = self._llm_client.chat_with_tools(messages, None)
        except Exception:
            return None
        if response.is_final and response.final:
            return response.final.strip()
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_assistant_message(response: dict[str, Any]) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": response.get("content") or ""}
        tool_calls = response.get("tool_calls")
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.get("id", f"call_{i}"),
                    "type": "function",
                    "function": tc.get("function", {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False)
                        if isinstance(tc.get("args"), dict) else str(tc.get("args", "{}")),
                    }),
                }
                for i, tc in enumerate(tool_calls)
            ]
        return msg
