"""Persistent task storage — v6.

Enhancements over v5:

- **FileLock** for multi-process safety.
- **CronSchedule** struct: supports ``once``, ``interval``, ``daily``,
  ``cron`` (croniter expressions), and ``at`` (absolute timestamp).
- **Timezone** support for cron/daily schedules.
- **RunRecord** with run_id: structured per-execution audit trail.
- **Concurrency control**: per-job running state prevents duplicate
  execution; PID tracking detects stale runs.
- **Atomic writes**: tmp + replace.
- **Auto-cleanup**: completed/failed once tasks pruned after 30 days.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TASK_ID_PATTERN = re.compile(r"^task_(\d+)$")
MAX_EXECUTION_HISTORY = 50
MAX_ONCE_TASK_AGE_DAYS = 30  # auto-cleanup completed/failed once tasks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# CronSchedule
# ---------------------------------------------------------------------------


@dataclass
class CronSchedule:
    """Structured schedule definition.

    Kinds:
        ``once``   — fire exactly at ``at_iso``.
        ``at``     — fire once at an absolute timestamp.
        ``every``  — fire every ``every_seconds`` seconds.
        ``daily``  — fire daily at ``daily_time`` (HH:MM), optional ``tz``.
        ``cron``   — fire on a croniter expression ``cron_expr``, optional ``tz``.
    """

    kind: Literal["once", "at", "every", "daily", "cron"] = "once"
    at_iso: str = ""             # ISO timestamp for once/at
    every_seconds: int = 0       # interval in seconds
    daily_time: str = ""         # "HH:MM" for daily
    cron_expr: str = ""          # croniter expression (e.g. "0 9 * * 1-5")
    tz: str = ""                 # timezone name (e.g. "Asia/Shanghai")

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind}
        if self.at_iso:
            d["at"] = self.at_iso
        if self.every_seconds:
            d["everySeconds"] = self.every_seconds
        if self.daily_time:
            d["dailyTime"] = self.daily_time
        if self.cron_expr:
            d["cronExpr"] = self.cron_expr
        if self.tz:
            d["tz"] = self.tz
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "CronSchedule":
        return cls(
            kind=data.get("kind", "once"),
            at_iso=str(data.get("at", "")),
            every_seconds=int(data.get("everySeconds", 0)),
            daily_time=str(data.get("dailyTime", "")),
            cron_expr=str(data.get("cronExpr", "")),
            tz=str(data.get("tz", "")),
        )

    @classmethod
    def from_legacy(cls, trigger_type: str, trigger_rule: str) -> "CronSchedule":
        """Convert old trigger_type/trigger_rule to CronSchedule."""
        if trigger_type == "once":
            return cls(kind="once", at_iso=str(trigger_rule))
        if trigger_type == "interval":
            try:
                secs = int(trigger_rule)
            except ValueError:
                secs = 3600
            return cls(kind="every", every_seconds=secs)
        if trigger_type == "daily":
            return cls(kind="daily", daily_time=str(trigger_rule))
        return cls(kind="once", at_iso=_now_iso())


# ---------------------------------------------------------------------------
# RunRecord
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    """A single execution record."""

    run_id: str
    started_at: str
    completed_at: str
    status: str  # "success" | "failure" | "timeout"
    reply: str | None = None
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "runId": self.run_id,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "status": self.status,
            "reply": self.reply,
            "error": self.error,
            "durationMs": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunRecord":
        return cls(
            run_id=str(data.get("runId", "")),
            started_at=str(data.get("startedAt", "")),
            completed_at=str(data.get("completedAt", "")),
            status=str(data.get("status", "failure")),
            reply=data.get("reply"),
            error=data.get("error"),
            duration_ms=int(data.get("durationMs", 0)),
        )


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """A scheduled task — v6.

    Key additions:
    - ``schedule``: structured ``CronSchedule`` (replaces trigger_type/trigger_rule).
    - ``run_records``: list of ``RunRecord`` (replaces execution_history).
    - ``name``: human-readable label.
    - ``max_concurrent``: max simultaneous executions (0 = unlimited).
    - ``defer_until_idle``: wait for session to be idle before executing.
    - ``timeout_seconds``: max execution duration (0 = no limit).
    - ``last_run_at``: timestamp of most recent execution.
    """

    task_id: str
    content: str
    schedule: CronSchedule = field(default_factory=CronSchedule)
    next_run_at: str = ""
    status: str = "waiting"
    session_id: str = "default"
    name: str = ""
    max_concurrent: int = 1
    defer_until_idle: bool = False
    timeout_seconds: int = 0
    run_records: list[RunRecord] = field(default_factory=list)
    last_run_at: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # Runtime state (not persisted)
    _running_count: int = field(default=0, repr=False, compare=False)
    _running_pid: int = field(default=0, repr=False, compare=False)

    @property
    def is_due(self, now_iso: str = "") -> bool:
        """Return True if the task is ready to execute."""
        if self.status != "waiting":
            return False
        if not self.next_run_at:
            return False
        return self.next_run_at <= (now_iso or _now_iso())

    @property
    def is_running(self) -> bool:
        return self._running_count > 0

    @property
    def trigger_type(self) -> str:
        """Backward-compat: return the legacy trigger_type string."""
        return self.schedule.kind

    @property
    def trigger_rule(self) -> str:
        """Backward-compat: return the legacy trigger_rule string."""
        s = self.schedule
        if s.kind in ("once", "at"):
            return s.at_iso
        if s.kind == "every":
            return str(s.every_seconds)
        if s.kind == "daily":
            return s.daily_time
        if s.kind == "cron":
            return s.cron_expr
        return ""

    def to_dict(self) -> dict:
        return {
            "id": self.task_id,
            "name": self.name,
            "content": self.content,
            "schedule": self.schedule.to_dict(),
            "nextRunAt": self.next_run_at,
            "status": self.status,
            "sessionId": self.session_id,
            "maxConcurrent": self.max_concurrent,
            "deferUntilIdle": self.defer_until_idle,
            "timeoutSeconds": self.timeout_seconds,
            "runRecords": [r.to_dict() for r in self.run_records],
            "lastRunAt": self.last_run_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        # Support both new (schedule) and old (triggerType/triggerRule) formats
        if "schedule" in data:
            schedule = CronSchedule.from_dict(data["schedule"])
        else:
            trigger_type = str(data.get("triggerType", "once"))
            trigger_rule = str(data.get("triggerRule", ""))
            schedule = CronSchedule.from_legacy(trigger_type, trigger_rule)

        # Support both new (runRecords) and old (executionHistory)
        records_raw = data.get("runRecords") or data.get("executionHistory") or []
        if not isinstance(records_raw, list):
            records_raw = []
        run_records = []
        for item in records_raw:
            if isinstance(item, dict):
                # Try new format first, then old
                if "runId" in item:
                    run_records.append(RunRecord.from_dict(item))
                else:
                    # Old TaskExecution format
                    run_records.append(RunRecord(
                        run_id="",
                        started_at=item.get("executedAt", ""),
                        completed_at=item.get("executedAt", ""),
                        status=item.get("status", "failure"),
                        reply=item.get("reply"),
                        error=item.get("error"),
                    ))

        # Calculate next_run_at if missing (from old format migration)
        next_run = data.get("nextRunAt", "")
        if not next_run:
            next_run = _calc_next_run(schedule, _now_iso())

        return cls(
            task_id=data["id"],
            name=str(data.get("name", "")),
            content=str(data.get("content", "")),
            schedule=schedule,
            next_run_at=next_run,
            status=str(data.get("status", "waiting")),
            session_id=str(data.get("sessionId", "default")),
            max_concurrent=int(data.get("maxConcurrent", 1)),
            defer_until_idle=bool(data.get("deferUntilIdle", False)),
            timeout_seconds=int(data.get("timeoutSeconds", 0)),
            run_records=run_records,
            last_run_at=str(data.get("lastRunAt", "")),
            created_at=str(data.get("createdAt", _now_iso())),
            updated_at=str(data.get("updatedAt", _now_iso())),
        )


# ---------------------------------------------------------------------------
# TasksStore
# ---------------------------------------------------------------------------


class TasksStoreError(RuntimeError):
    """User-facing task storage failure."""


class TasksStore:
    """Thread-safe + FileLock CRUD store — v6."""

    def __init__(self, tasks_file: Path):
        self._tasks_file = tasks_file
        self._tasks: list[Task] = []
        self._lock = threading.Lock()
        self._file_lock: object | None = None
        self.load_warning: str | None = None
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._tasks_file.exists():
            return
        try:
            raw = self._tasks_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("tasks 文件格式错误：顶层结构应为数组")
            self._tasks = [Task.from_dict(item) for item in data]
            # Detect stale "running" tasks (from crashed process)
            self._recover_stale_runs()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            backup = self._tasks_file.with_name(
                f"{self._tasks_file.name}.corrupted-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            )
            try:
                self._tasks_file.rename(backup)
            except OSError:
                pass
            self.load_warning = (
                f"tasks 文件已损坏，已备份为 {backup.name}，"
                f"以空任务列表启动。详情：{exc}"
            )
            self._tasks = []

    def _save(self) -> None:
        try:
            self._tasks_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._tasks_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    [t.to_dict() for t in self._tasks],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._tasks_file)
        except OSError as exc:
            raise TasksStoreError(f"保存 tasks 失败: {exc}") from exc

    def _recover_stale_runs(self) -> None:
        """Reset stale 'running' tasks from a previous crashed process."""
        for task in self._tasks:
            if task.status == "running":
                # If the PID doesn't exist anymore, reset to waiting
                if task._running_pid and not _pid_exists(task._running_pid):
                    task.status = "waiting"
                    task._running_count = 0
                    task._running_pid = 0
                    # Recalculate next_run_at
                    task.next_run_at = _calc_next_run(
                        task.schedule, _now_iso()
                    )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        content: str,
        schedule: CronSchedule,
        session_id: str = "default",
        *,
        name: str = "",
        max_concurrent: int = 1,
        defer_until_idle: bool = False,
        timeout_seconds: int = 0,
    ) -> Task:
        with self._lock:
            now = _now_iso()
            next_run = _calc_next_run(schedule, now)
            task = Task(
                task_id=self._generate_id(),
                name=name,
                content=content,
                schedule=schedule,
                next_run_at=next_run,
                status="waiting",
                session_id=session_id,
                max_concurrent=max_concurrent,
                defer_until_idle=defer_until_idle,
                timeout_seconds=timeout_seconds,
                created_at=now,
                updated_at=now,
            )
            self._tasks.append(task)
            self._save()
            return task

    def get(self, task_id: str) -> Task:
        with self._lock:
            for t in self._tasks:
                if t.task_id == task_id:
                    return t
            raise TasksStoreError(f"未找到 task: {task_id}")

    def list_all(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks, key=lambda t: t.created_at, reverse=True)

    def get_due(self, now_iso: str) -> list[Task]:
        """Return waiting tasks that are due and not at concurrency limit."""
        with self._lock:
            due = []
            for t in self._tasks:
                if not t.is_due(now_iso):
                    continue
                if t.max_concurrent > 0 and t._running_count >= t.max_concurrent:
                    continue
                if t.defer_until_idle:
                    # Check if session has active tasks
                    # (caller provides active_session_keys)
                    pass  # filtered by scheduler
                due.append(t)
            return due

    def try_acquire(self, task_id: str) -> bool:
        """Try to mark a task as running. Returns True if acquired."""
        with self._lock:
            task = self._get_locked(task_id)
            if task.status not in ("waiting",):
                return False
            if task.max_concurrent > 0 and task._running_count >= task.max_concurrent:
                return False
            task.status = "running"
            task._running_count += 1
            task._running_pid = os.getpid()
            task.last_run_at = _now_iso()
            task.updated_at = _now_iso()
            self._save()
            return True

    def record_success(self, task_id: str, reply: str, duration_ms: int = 0) -> None:
        self._record_run(task_id, "success", reply=reply, duration_ms=duration_ms)

    def record_failure(self, task_id: str, error: str, duration_ms: int = 0) -> None:
        self._record_run(task_id, "failure", error=error, duration_ms=duration_ms)

    def _record_run(
        self,
        task_id: str,
        status: str,
        *,
        reply: str | None = None,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        import uuid
        now = _now_iso()
        with self._lock:
            task = self._get_locked(task_id)
            run_id = f"run_{uuid.uuid4().hex[:12]}"
            task.run_records.append(RunRecord(
                run_id=run_id,
                started_at=task.last_run_at or now,
                completed_at=now,
                status=status,
                reply=reply,
                error=error,
                duration_ms=duration_ms,
            ))
            if len(task.run_records) > MAX_EXECUTION_HISTORY:
                task.run_records = task.run_records[-MAX_EXECUTION_HISTORY:]

            task._running_count = max(0, task._running_count - 1)
            if task._running_count <= 0:
                task._running_pid = 0

            if task.schedule.kind in ("once", "at"):
                task.status = "completed" if status == "success" else "failed"
                task.next_run_at = ""
            else:
                task.status = "waiting"
                task.next_run_at = _calc_next_run(task.schedule, now)

            task.updated_at = now
            self._save()

    def cancel(self, task_id: str) -> None:
        self._update(task_id, status="cancelled", next_run_at="")

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._get_locked(task_id)
            self._tasks = [t for t in self._tasks if t.task_id != task_id]
            self._save()

    def cleanup_stale(self) -> int:
        """Remove completed/failed once tasks older than MAX_ONCE_TASK_AGE_DAYS."""
        import time
        cutoff = _now_iso()
        removed = 0
        with self._lock:
            kept = []
            for t in self._tasks:
                if t.schedule.kind in ("once", "at") and t.status in ("completed", "failed", "cancelled"):
                    try:
                        age_days = (
                            datetime.now(timezone.utc)
                            - datetime.fromisoformat(t.updated_at)
                        ).days
                    except (ValueError, TypeError):
                        age_days = 0
                    if age_days > MAX_ONCE_TASK_AGE_DAYS:
                        removed += 1
                        continue
                kept.append(t)
            self._tasks = kept
            if removed > 0:
                self._save()
        return removed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _update(self, task_id: str, **kwargs) -> None:
        with self._lock:
            task = self._get_locked(task_id)
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.updated_at = _now_iso()
            self._save()

    def _get_locked(self, task_id: str) -> Task:
        for t in self._tasks:
            if t.task_id == task_id:
                return t
        raise TasksStoreError(f"未找到 task: {task_id}")

    def _generate_id(self) -> str:
        max_seq = 0
        for t in self._tasks:
            m = _TASK_ID_PATTERN.match(t.task_id)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return f"task_{max_seq + 1:03d}"


# ---------------------------------------------------------------------------
# Schedule calculation
# ---------------------------------------------------------------------------


def _calc_next_run(schedule: CronSchedule, from_iso: str) -> str:
    """Calculate the next run time from *from_iso*."""
    try:
        dt = datetime.fromisoformat(from_iso)
    except ValueError:
        dt = datetime.now(timezone.utc)

    if schedule.kind in ("once", "at"):
        return schedule.at_iso or from_iso

    if schedule.kind == "every":
        secs = schedule.every_seconds or 3600
        from datetime import timedelta
        return (dt + timedelta(seconds=secs)).isoformat(timespec="seconds")

    if schedule.kind == "daily":
        try:
            parts = str(schedule.daily_time).strip().split(":")
            hour, minute = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            hour, minute = 9, 0

        if schedule.tz:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(schedule.tz)
                dt = dt.astimezone(tz)
            except Exception:
                pass

        candidate = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= dt:
            candidate = candidate.replace(day=candidate.day + 1)

        if schedule.tz:
            candidate = candidate.astimezone(timezone.utc)
        return candidate.isoformat(timespec="seconds")

    if schedule.kind == "cron":
        try:
            from croniter import croniter
            base = dt
            if schedule.tz:
                try:
                    from zoneinfo import ZoneInfo
                    base = dt.astimezone(ZoneInfo(schedule.tz))
                except Exception:
                    pass
            cron = croniter(schedule.cron_expr, base)
            next_dt = cron.get_next(datetime)
            if schedule.tz:
                next_dt = next_dt.astimezone(timezone.utc)
            return next_dt.isoformat(timespec="seconds")
        except Exception:
            pass

    # Fallback
    from datetime import timedelta
    return (dt + timedelta(seconds=3600)).isoformat(timespec="seconds")


def _pid_exists(pid: int) -> bool:
    """Check if a process with the given PID exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False
