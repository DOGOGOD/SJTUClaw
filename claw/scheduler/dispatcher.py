"""Shared cron dispatcher — routes scheduled jobs to the agent loop.

Used by CLI (``claw/main.py``) and Gateway (``claw/gateway/server.py``)
so that cron job execution logic is defined once.

Callers supply optional hooks to customise heartbeat, session recovery,
delivery, and active-session tracking for their channel (CLI / QQ / Web).
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Awaitable, Callable, Optional

from claw.agent.loop import run_agent_turn
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient
from claw.session.store import SessionStore
from claw.tools.base import ToolRegistry

# -- Hook signatures ----------------------------------------------------------

HeartbeatHook = Callable[..., Awaitable[Optional[str]]]
"""Called for heartbeat jobs; receives the job, returns optional result."""

DeliveryHook = Callable[
    [str, str, str, Optional[dict[str, Any]]],  # channel, chat_id, reply, metadata
    Awaitable[None],
]
"""Called to deliver a reply back to the origin channel."""

SessionResolveHook = Callable[[str, str], Optional[str]]
"""Called to resolve a session_key when it doesn't exist on disk.
Receives (session_key, origin_chat_id); returns the resolved key or None."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_cron_dispatcher(
    *,
    session_store: SessionStore,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
    llm_client: LLMClient,
    set_turn_session_id: Callable[[Optional[str]], None],
    update_cron_context: Callable[..., None],
    on_heartbeat: HeartbeatHook | None = None,
    on_deliver: DeliveryHook | None = None,
    on_session_resolve: SessionResolveHook | None = None,
    on_turn_active: Callable[[str], None] | None = None,
    on_turn_idle: Callable[[str], None] | None = None,
    on_turn_start: Callable[[str, str], None] | None = None,
    on_turn_finish: Callable[[str, bool, Optional[str]], None] | None = None,
    event_handler: Callable[[str, Any], None] | None = None,
):
    """Build an async dispatcher for cron job execution.

    Parameters
    ----------
    session_store : SessionStore
    context_builder : ContextBuilder
    tool_registry : ToolRegistry
    llm_client : LLMClient
    set_turn_session_id : callable
        Set the per-thread session ID so tool handlers see the correct session.
    update_cron_context : callable
        Update the cron tool's session context before the agent turn.
    on_heartbeat : callable | None
        Invoked for heartbeat jobs.  Called as ``await on_heartbeat(job)``.
    on_deliver : callable | None
        Invoked to push the reply back to the origin channel.
        Called as ``await on_deliver(channel, chat_id, reply, metadata)``.
    on_session_resolve : callable | None
        If the session_key is not found on disk, this hook can return an
        alternative key (e.g. recovering a QQ session after gateway restart).
    on_turn_active : callable | None
        Called just before ``run_agent_turn`` with *session_key*.
    on_turn_idle : callable | None
        Called after ``run_agent_turn`` completes (in the ``finally`` block).
    on_turn_start : callable | None
        Called with ``(session_key, message)`` right before the agent turn
        begins — e.g. to update desktop-pet state.
    on_turn_finish : callable | None
        Called with ``(session_key, failed, reply)`` after the agent turn
        completes — e.g. to trigger a pet animation + bubble.
    event_handler : callable | None
        Called with ``(session_key, event)`` for each ``TurnEvent`` emitted
        during the agent turn (mirrors ``/chat``'s ``event_callback``).
    """

    async def dispatch(job) -> str | None:
        print(
            f"[cron] dispatching job '{job.name}' ({job.id}) "
            f"kind={job.payload.kind} session={job.payload.session_key} "
            f"channel={job.payload.origin_channel}"
        )

        # -- Heartbeat ---------------------------------------------------------
        if job.name == "heartbeat" and on_heartbeat is not None:
            return await on_heartbeat(job)

        # -- User-created agent_turn jobs --------------------------------------
        if job.payload.kind == "agent_turn" and job.payload.message:
            try:
                sid = job.payload.session_key or "default"
                print(
                    f"[cron] agent_turn job '{job.name}' "
                    f"session={sid} message={job.payload.message[:80]}..."
                )

                # Resolve session (use most recent if not found, create only as last resort)
                if not session_store.exists(sid):
                    if on_session_resolve is not None:
                        recovered = on_session_resolve(
                            sid, job.payload.origin_chat_id or ""
                        )
                        if recovered is not None:
                            sid = recovered
                    if not session_store.exists(sid):
                        # 使用最近的 session 而非新建
                        recent = session_store.list_summaries()
                        if recent:
                            sid = recent[0].session_id
                        if not session_store.exists(sid):
                            session_store.create_session(session_id=sid)

                if on_turn_active is not None:
                    on_turn_active(sid)
                if on_turn_start is not None:
                    on_turn_start(sid, job.payload.message)

                def _run_bound_turn():
                    """Run the turn with all thread/context-local state bound."""
                    set_turn_session_id(sid)
                    update_cron_context(
                        sid,
                        job.payload.origin_chat_id or sid,
                        channel=job.payload.origin_channel or "gateway",
                        metadata=job.payload.origin_metadata or {},
                    )

                    cron_tool = tool_registry.get_tool("cron")
                    cron_token = None
                    if cron_tool is not None and hasattr(cron_tool, "set_cron_context"):
                        cron_token = cron_tool.set_cron_context(True)

                    try:
                        return run_agent_turn(
                            sid,
                            f"[定时任务: {job.name}]\n\n{job.payload.message}",
                            session_store=session_store,
                            context_builder=context_builder,
                            tool_registry=tool_registry,
                            llm_client=llm_client,
                            input_event="cron_trigger",
                            event_callback=(
                                lambda event: event_handler(sid, event)
                                if event_handler is not None
                                else None
                            ),
                        )
                    finally:
                        if cron_token is not None and hasattr(
                            cron_tool, "reset_cron_context"
                        ):
                            cron_tool.reset_cron_context(cron_token)
                        set_turn_session_id(None)

                failed = False
                reply: str | None = None
                try:
                    reply = await asyncio.to_thread(_run_bound_turn)
                except Exception:
                    failed = True
                    raise
                finally:
                    if on_turn_idle is not None:
                        on_turn_idle(sid)
                    if on_turn_finish is not None:
                        on_turn_finish(sid, failed, reply)

                # Deliver reply via the origin channel
                if reply:
                    print(
                        f"[cron] job '{job.name}' reply={len(reply)} chars "
                        f"origin_channel={job.payload.origin_channel}"
                    )
                if reply and job.payload.origin_channel and on_deliver is not None:
                    await on_deliver(
                        job.payload.origin_channel,
                        job.payload.origin_chat_id or sid,
                        reply,
                        job.payload.origin_metadata or {},
                    )
                elif reply:
                    print(
                        f"[cron] job '{job.name}' "
                        f"无 origin_channel，跳过投递"
                    )

                return reply
            except Exception as e:
                print(f"[cron] job '{job.name}' 执行失败: {e}")
                traceback.print_exc()
                return f"定时任务 '{job.name}' 执行失败: {e}"

        return None

    return dispatch


__all__ = [
    "create_cron_dispatcher",
    "HeartbeatHook",
    "DeliveryHook",
    "SessionResolveHook",
]
