"""Cron job callbacks for Heartbeat.

These are the handler functions called by CronService when system jobs fire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claw.scheduler.types import CronJob


# ---------------------------------------------------------------------------
# Heartbeat callback — HEARTBEAT.md monitoring
# ---------------------------------------------------------------------------

_HEARTBEAT_PREAMBLE = (
    "你是一个后台监控助手。HEARTBEAT.md 中列出了需要周期性检查的任务。"
    "请审阅以下内容，如果有需要报告的事项，简洁地汇报。"
    "如果一切正常，仅回复 'All clear.'。\n\n"
)


class HeartbeatCallback:
    """Heartbeat callback — checks HEARTBEAT.md for active tasks.

    Reads ``HEARTBEAT.md`` from the workspace and dispatches an agent turn
    if there are active tasks to report on.
    """

    def __init__(
        self,
        workspace_path: Path,
        session_store,
        context_builder,
        tool_registry,
        llm_client,
        keep_recent_messages: int = 8,
        default_session_key: str = "heartbeat",
    ):
        self._workspace_path = workspace_path
        self._session_store = session_store
        self._context_builder = context_builder
        self._tool_registry = tool_registry
        self._llm_client = llm_client
        self._keep_recent = keep_recent_messages
        self._default_session_key = default_session_key
        self._heartbeat_file = workspace_path / "HEARTBEAT.md"

    async def __call__(self, job: CronJob) -> str | None:
        try:
            content = self._heartbeat_file.read_text(encoding="utf-8")
        except OSError:
            return None  # No HEARTBEAT.md — nothing to do

        if not self._has_active_tasks(content):
            return None

        from claw.agent.loop import run_agent_turn

        prompt = _HEARTBEAT_PREAMBLE + content

        try:
            reply = run_agent_turn(
                self._default_session_key,
                prompt,
                session_store=self._session_store,
                context_builder=self._context_builder,
                tool_registry=self._tool_registry,
                llm_client=self._llm_client,
            )

            # Trim heartbeat session history
            try:
                session = self._session_store.get(self._default_session_key)
                if len(session.messages) > self._keep_recent:
                    session.messages = session.messages[-self._keep_recent:]
                    self._session_store.save(session)
            except Exception:
                pass

            if reply and reply.strip() != "All clear.":
                return reply
            return None
        except Exception as e:
            return f"Heartbeat 失败: {e}"

    @staticmethod
    def _has_active_tasks(content: str) -> bool:
        """True if HEARTBEAT.md has task lines, ignoring headers and comments."""
        in_comment = False
        in_active_section = False
        for line in content.splitlines():
            stripped = line.strip()
            if in_comment:
                if "-->" in stripped:
                    in_comment = False
                continue
            if stripped.startswith("<!--"):
                in_comment = True
                continue

            if not in_active_section:
                if stripped.lower().startswith("## active tasks"):
                    in_active_section = True
                continue

            if stripped.startswith("#") or stripped.startswith("<!--"):
                continue
            if not stripped:
                continue
            return True
        return False


# ---------------------------------------------------------------------------
# Helper — create Heartbeat system job descriptor
# ---------------------------------------------------------------------------


def make_heartbeat_system_job(heartbeat_cfg) -> CronJob:
    """Build the Heartbeat system job from config."""
    from claw.scheduler.types import CronPayload, CronSchedule

    return CronJob(
        id="heartbeat",
        name="heartbeat",
        schedule=CronSchedule(
            kind="every",
            every_ms=heartbeat_cfg.interval_s * 1000,
        ),
        payload=CronPayload(kind="system_event"),
    )
