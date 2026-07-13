"""Cron tool for scheduling reminders and tasks."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from claw.scheduler.service import CronService
from claw.scheduler.types import CronJob, CronJobState, CronSchedule
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
        # Wire the handler so ToolRegistry.execute_by_name() can call it.
        self.handler = self.execute_sync

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
            "Schedule reminders and tasks. Actions: add, list, remove.\n"
            "\n"
            "WHEN TO USE EACH TIMING PARAMETER (action='add'):\n"
            "- User says \"in X minutes/seconds/hours\" or \"X minutes later\"\n"
            "  → ONE-SHOT: use delay_seconds (fires once, then auto-deletes).\n"
            "- User says \"every X minutes/seconds/hours\" or \"每隔\"\n"
            "  → RECURRING: use every_seconds (repeats forever until removed).\n"
            "- User says \"at 9am every day\" or \"every Monday at 3pm\"\n"
            "  → RECURRING: use cron_expr (e.g. '0 9 * * *' for daily 9am).\n"
            "- User says \"at 2026-07-15 14:30\" (specific absolute time)\n"
            "  → ONE-SHOT: use at with ISO datetime.\n"
            f"Default timezone: {self._default_timezone}."
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
                        "(e.g., 'Send a reminder: it has been 10 minutes!' or 'Check system status and report'). "
                        "Not used for action='list' or action='remove'."
                    ),
                },
                "delay_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "ONE-SHOT delay in seconds. The job fires ONCE after this many seconds, "
                        "then auto-deletes. Use when the user says \"in X minutes\", "
                        "\"X minutes later\", \"after X seconds\", \"remind me in 30 minutes\". "
                        "Example: \"remind me in 5 minutes\" → delay_seconds=300. "
                        "Do NOT use this for recurring/periodic tasks."
                    ),
                },
                "every_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "RECURRING interval in seconds. The job fires REPEATEDLY every N seconds "
                        "until manually removed. Use when the user says \"every X minutes\", "
                        "\"每隔X分钟\", \"periodically every X seconds\". "
                        "Example: \"remind me every 10 minutes\" → every_seconds=600. "
                        "Do NOT use this for one-shot/in-N-seconds tasks — use delay_seconds instead."
                    ),
                },
                "cron_expr": {
                    "type": "string",
                    "description": (
                        "Cron expression for RECURRING scheduled tasks with complex patterns, "
                        "e.g. '0 9 * * *' for daily at 9am, '*/30 * * * *' for every 30 minutes, "
                        "'0 9 * * 1' for every Monday at 9am."
                    ),
                },
                "tz": {
                    "type": "string",
                    "description": (
                        "Optional IANA timezone for cron expressions (e.g. 'Asia/Shanghai', 'America/Vancouver'). "
                        "When omitted with cron_expr, the tool's default timezone applies."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "ONE-SHOT absolute time as ISO datetime (e.g. '2026-02-12T10:30:00'). "
                        "Use when the user gives a specific date+time like \"at 3pm tomorrow\". "
                        "Naive values use the tool's default timezone. Job auto-deletes after firing."
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
        except Exception:
            return f"Error: unknown timezone '{tz}'"
        return None

    def _add_job(self, args: dict[str, Any]) -> ToolResult:
        name = args.get("name")
        message = str(args.get("message", ""))
        delay_seconds = args.get("delay_seconds")
        every_seconds = args.get("every_seconds")
        cron_expr = args.get("cron_expr")
        tz = args.get("tz")
        at = args.get("at")

        timing_fields = [
            key
            for key in ("delay_seconds", "every_seconds", "cron_expr", "at")
            if args.get(key) is not None
        ]
        if len(timing_fields) != 1:
            return ToolResult(
                ok=False,
                error=(
                    "Error: action='add' requires exactly one timing parameter: "
                    "delay_seconds, every_seconds, cron_expr, or at"
                ),
            )

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
        import time as _time
        now_ms = int(_time.time() * 1000)
        delete_after = False
        if delay_seconds is not None:
            if isinstance(delay_seconds, bool):
                return ToolResult(ok=False, error="Error: delay_seconds must be greater than 0")
            try:
                delay_value = int(delay_seconds)
            except (TypeError, ValueError, OverflowError):
                return ToolResult(ok=False, error="Error: delay_seconds must be a positive integer")
            if delay_value <= 0:
                return ToolResult(ok=False, error="Error: delay_seconds must be greater than 0")
            # One-shot delayed execution: fire once after N seconds, auto-delete.
            delay_ms = delay_value * 1000
            schedule = CronSchedule(kind="at", at_ms=now_ms + delay_ms)
            delete_after = True
        elif every_seconds is not None:
            if isinstance(every_seconds, bool):
                return ToolResult(ok=False, error="Error: every_seconds must be greater than 0")
            try:
                every_value = int(every_seconds)
            except (TypeError, ValueError, OverflowError):
                return ToolResult(ok=False, error="Error: every_seconds must be a positive integer")
            if every_value <= 0:
                return ToolResult(ok=False, error="Error: every_seconds must be greater than 0")
            # Recurring execution every N seconds.
            schedule = CronSchedule(kind="every", every_ms=every_value * 1000)
        elif cron_expr is not None:
            if not str(cron_expr).strip():
                return ToolResult(ok=False, error="Error: cron_expr cannot be empty")
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return ToolResult(ok=False, error=err)
            schedule = CronSchedule(kind="cron", expr=str(cron_expr), tz=effective_tz)
        elif at is not None:
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
            if at_ms <= now_ms:
                return ToolResult(ok=False, error="Error: 'at' must be a future datetime")
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return ToolResult(
                ok=False,
                error=(
                    "Error: either delay_seconds, every_seconds, cron_expr, or at is required. "
                    "Use delay_seconds for one-shot (\"in 5 minutes\"), "
                    "every_seconds for recurring (\"every 10 minutes\"), "
                    "cron_expr for complex schedules (\"daily at 9am\"), "
                    "or at for a specific absolute time."
                ),
            )

        try:
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
        except ValueError as exc:
            return ToolResult(ok=False, error=f"Error: invalid schedule: {exc}")
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
            # Show as a one-shot reminder: either a delay or an absolute time
            import time as _time
            now_ms = int(_time.time() * 1000)
            if schedule.at_ms > now_ms:
                remaining_s = (schedule.at_ms - now_ms) // 1000
                if remaining_s < 120:
                    return f"in {remaining_s}s"
                if remaining_s < 7200:
                    return f"in {remaining_s // 60}min"
                return f"in {remaining_s // 3600}h"
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
            return ToolResult(
                ok=False,
                error=(
                    f"Cannot remove job `{job_id}`.\n"
                    "This is a protected system-managed cron job."
                ),
            )
        return ToolResult(ok=False, error=f"Job {job_id} not found")
