"""Agent loop hooks — extensibility system.

Hooks allow external code to observe and influence the agent loop at
key lifecycle points without modifying the core loop logic.

Lifecycle:
    before_session → before_turn → before_messages → after_messages →
    before_llm → after_llm → before_tools → after_tool → after_tools →
    before_save → after_save → after_turn → after_session

Usage::

    class LoggingHook(AgentHook):
        def before_llm(self, ctx):
            print(f"[hook] Calling LLM, iteration={ctx.iteration}")

    loop = AgentLoop(hooks=[LoggingHook()])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Hook context
# ---------------------------------------------------------------------------


@dataclass
class HookContext:
    """Immutable snapshot of the agent state at a hook point.

    Fields may be None if not yet available at the current lifecycle stage.
    """

    iteration: int = 0
    session_key: str = ""
    session_id: str = ""
    channel: str = ""
    chat_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str = ""
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hook interface
# ---------------------------------------------------------------------------


class AgentHook:
    """Base class for agent loop hooks.

    Each method is a no-op by default. Subclass and override the methods
    you need. Exceptions raised by hooks are caught and logged — they
    never crash the agent loop.

    All methods receive a ``HookContext`` and may mutate it to influence
    downstream behavior (e.g., ``after_llm`` can modify the response).
    """

    # -- Session-level ---------------------------------------------------

    def before_session(self, ctx: HookContext) -> None:
        """Called once before processing begins for a session."""

    def after_session(self, ctx: HookContext) -> None:
        """Called once after all turns in a session are complete."""

    # -- Turn-level ------------------------------------------------------

    def before_turn(self, ctx: HookContext) -> None:
        """Called at the start of each tool-calling iteration."""

    def after_turn(self, ctx: HookContext) -> None:
        """Called at the end of each tool-calling iteration."""

    # -- Message building ------------------------------------------------

    def before_build_messages(self, ctx: HookContext) -> None:
        """Called before ContextBuilder.build_messages()."""

    def after_build_messages(self, ctx: HookContext) -> None:
        """Called after ContextBuilder.build_messages()."""

    # -- LLM call --------------------------------------------------------

    def before_llm(self, ctx: HookContext) -> None:
        """Called just before the LLM API request."""

    def after_llm(self, ctx: HookContext) -> None:
        """Called just after receiving the LLM response.
        *ctx.messages* includes the new assistant message."""

    # -- Tool execution --------------------------------------------------

    def before_execute_tools(self, ctx: HookContext) -> None:
        """Called before executing a batch of tool calls."""

    def before_execute_tool(self, ctx: HookContext, tool_name: str, tool_args: dict) -> None:
        """Called before executing a single tool."""

    def after_execute_tool(self, ctx: HookContext, tool_name: str, tool_args: dict, result: Any) -> None:
        """Called after executing a single tool."""

    def after_execute_tools(self, ctx: HookContext) -> None:
        """Called after executing all tool calls in a batch."""

    # -- Save -----------------------------------------------------------

    def before_save(self, ctx: HookContext) -> None:
        """Called before persisting the turn to session storage."""

    def after_save(self, ctx: HookContext) -> None:
        """Called after persisting the turn to session storage."""

    # -- Error ----------------------------------------------------------

    def on_error(self, ctx: HookContext) -> None:
        """Called when an error occurs at any lifecycle point."""


# ---------------------------------------------------------------------------
# Hook runner
# ---------------------------------------------------------------------------


class HookRunner:
    """Runs a list of hooks, catching and logging exceptions."""

    def __init__(self, hooks: list[AgentHook] | None = None):
        self._hooks: list[AgentHook] = list(hooks) if hooks else []

    def add(self, hook: AgentHook) -> None:
        self._hooks.append(hook)

    def remove(self, hook: AgentHook) -> None:
        self._hooks.remove(hook)

    def _run(self, method_name: str, ctx: HookContext, *args, **kwargs) -> None:
        for hook in self._hooks:
            try:
                method = getattr(hook, method_name, None)
                if method is not None:
                    method(ctx, *args, **kwargs)
            except Exception:
                import traceback
                traceback.print_exc()

    # Convenience methods
    def before_session(self, ctx: HookContext) -> None:
        self._run("before_session", ctx)

    def after_session(self, ctx: HookContext) -> None:
        self._run("after_session", ctx)

    def before_turn(self, ctx: HookContext) -> None:
        self._run("before_turn", ctx)

    def after_turn(self, ctx: HookContext) -> None:
        self._run("after_turn", ctx)

    def before_build_messages(self, ctx: HookContext) -> None:
        self._run("before_build_messages", ctx)

    def after_build_messages(self, ctx: HookContext) -> None:
        self._run("after_build_messages", ctx)

    def before_llm(self, ctx: HookContext) -> None:
        self._run("before_llm", ctx)

    def after_llm(self, ctx: HookContext) -> None:
        self._run("after_llm", ctx)

    def before_execute_tools(self, ctx: HookContext) -> None:
        self._run("before_execute_tools", ctx)

    def before_execute_tool(self, ctx: HookContext, tool_name: str, tool_args: dict) -> None:
        self._run("before_execute_tool", ctx, tool_name, tool_args)

    def after_execute_tool(self, ctx: HookContext, tool_name: str, tool_args: dict, result: Any) -> None:
        self._run("after_execute_tool", ctx, tool_name, tool_args, result)

    def after_execute_tools(self, ctx: HookContext) -> None:
        self._run("after_execute_tools", ctx)

    def before_save(self, ctx: HookContext) -> None:
        self._run("before_save", ctx)

    def after_save(self, ctx: HookContext) -> None:
        self._run("after_save", ctx)

    def on_error(self, ctx: HookContext) -> None:
        self._run("on_error", ctx)
