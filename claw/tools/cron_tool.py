"""Cron tool for scheduling reminders and tasks."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from claw.cron.service import CronService
from claw.cron.types import CronJob, CronJobState, CronSchedule
from claw.tools.base import Tool, ToolResult


class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks.

    Actions: add, list, remove.
    """

    def __init__(
        self,
        cron_service: CronService,
        default_timezone: str = "UTC",
    ):
        self._cron = cron_service
        self._default_timezone = default_timezone
        self._session_key: ContextVar[str] = ContextVar("cron_session_key", default="")
        self._origin_channel: ContextVar[str] = ContextVar("cron_origin_channel", default="")
        self._origin_chat_id: ContextVar[str] = ContextVar("cron_origin_chat_id", default="")
        self._origin_metadata: ContextVar[dict[str, Any] | None] = ContextVar(
            "cron_origin_metadata",
            default=None,
        )
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(
        self,
        session_key: str = "",
        channel: str = "",
        chat_id: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Set the current session context for scheduled cron job ownership."""
        self._session_key.set(session_key or "")
        self._origin_channel.set(channel or "")
        self._origin_chat_id.set(chat_id or "")
        self._origin_metadata.set(dict(metadata or {}))

    def set_cron_context(self, active: bool) -> Any:
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    # -- Tool interface -------------------------------------------------------

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "Action to perform",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional short human-readable label for the job "
                        "(e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": (
                        "REQUIRED when action='add'. Instruction for the agent to execute when the job triggers "
                        "(e.g., 'Send a reminder to WeChat: xxx' or 'Check system status and report'). "
                        "Not used for action='list' or action='remove'."
                    ),
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds (for recurring tasks)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression like '0 9 * * *' (for scheduled tasks)",
                },
                "tz": {
                    "type": "string",
                    "description": (
                        "Optional IANA timezone for cron expressions (e.g. 'America/Vancouver'). "
                        "When omitted with cron_expr, the tool's default timezone applies."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
                        "Naive values use the tool's default timezone."
                    ),
                },
                "job_id": {
                    "type": "string",
                    "description": "REQUIRED when action='remove'. Job ID to remove (obtain via action='list').",
                },
            },
            "required": ["action"],
        }

    @property
    def safety_level(self) -> str:
        return "read_only"

    def execute_sync(self, args: dict[str, Any]) -> ToolResult:
        action = args.get("action", "")

        if action == "add":
            if self._in_cron_context.get():
                return ToolResult(
                    ok=False,
                    error="Error: cannot schedule new jobs from within a cron job execution",
                )
            return self._add_job(args)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(args.get("job_id"))
        else:
            return ToolResult(ok=False, error=f"Unknown action: {action}")

    # -- Internal -------------------------------------------------------------

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _add_job(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        message = str(args.get("message", ""))
        every_seconds = args.get("every_seconds")
        cron_expr = args.get("cron_expr")
        tz = args.get("tz")
        at = args.get("at")

        if not message:
            return ToolResult(
                ok=False,
                error=(
                    "Error: cron action='add' requires a non-empty 'message' parameter "
                    "describing what to do when the job triggers "
                    "(e.g. the reminder text). Retry including message=\"...\"."
                ),
            )

        session_key = self._session_key.get()
        if not session_key:
            return ToolResult(
                ok=False,
                error="Error: scheduled cron jobs must be created from a chat session",
            )

        origin_channel = self._origin_channel.get()
        origin_chat_id = self._origin_chat_id.get()
        if not origin_channel or not origin_chat_id:
            return ToolResult(
                ok=False,
                error="Error: scheduled cron jobs must be created from a chat session",
            )

        if tz and not cron_expr:
            return ToolResult(
                ok=False,
                error="Error: tz can only be used with cron_expr",
            )

        if tz:
            if err := self._validate_timezone(tz):
                return ToolResult(ok=False, error=err)

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=int(every_seconds) * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return ToolResult(ok=False, error=err)
            schedule = CronSchedule(kind="cron", expr=str(cron_expr), tz=effective_tz)
        elif at:
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(str(at))
            except ValueError:
                return ToolResult(
                    ok=False,
                    error=f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS",
                )
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return ToolResult(ok=False, error=err)
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return ToolResult(
                ok=False,
                error="Error: either every_seconds, cron_expr, or at is required",
            )

        job = self._cron.add_job(
            name=name or message[:30],
            schedule=schedule,
            message=message,
            delete_after_run=delete_after,
            session_key=session_key,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_metadata=dict(self._origin_metadata.get() or {}),
        )
        return ToolResult(
            ok=True,
            content=f"Created job '{job.name}' (id: {job.id})",
        )

    # -- Formatting -----------------------------------------------------------

    @staticmethod
    def _display_timezone(schedule: CronSchedule, fallback: str = "UTC") -> str:
        """Pick the most human-meaningful timezone for display."""
        return schedule.tz or fallback

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        if job.name == "heartbeat":
            return "Heartbeat monitoring for active tasks in HEARTBEAT.md."
        return "System-managed internal job."

    def _list_jobs(self) -> ToolResult:
        jobs = self._cron.list_jobs()
        if not jobs:
            return ToolResult(ok=True, content="No scheduled jobs.")

        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return ToolResult(ok=True, content="Scheduled jobs:\n" + "\n".join(lines))

    def _remove_job(self, job_id: str | None) -> ToolResult:
        if not job_id:
            return ToolResult(ok=False, error="Error: job_id is required for remove")
        result = self._cron.remove_job(str(job_id))
        if result == "removed":
            return ToolResult(ok=True, content=f"Removed job {job_id}")
        if result == "protected":
            job = self._cron.get_job(str(job_id))
            if job and job.name == "dream":
                return ToolResult(
                    ok=False,
                    error=(
                        "Cannot remove job `dream`.\n"
                        "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                        "It remains visible so you can inspect it, but it cannot be removed."
                    ),
                )
            return ToolResult(
                ok=False,
                error=(
                    f"Cannot remove job `{job_id}`.\n"
                    "This is a protected system-managed cron job."
                ),
            )
        return ToolResult(ok=False, error=f"Job {job_id} not found")
