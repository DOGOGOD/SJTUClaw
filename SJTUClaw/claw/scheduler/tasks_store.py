"""Persistent task storage.

Tasks are saved as a JSON array at ``data/tasks/tasks.json``.
The store is thread-safe for use by both the Gateway (HTTP handlers)
and the Scheduler (background thread).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_TASK_ID_PATTERN = re.compile(r"^task_(\d+)$")
MAX_EXECUTION_HISTORY = 50  # Keep at most the last 50 executions per task


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TaskExecution:
    """A single execution record."""

    executed_at: str
    status: str  # "success" | "failure"
    reply: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "executedAt": self.executed_at,
            "status": self.status,
            "reply": self.reply,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskExecution":
        return cls(
            executed_at=data.get("executedAt", _now_iso()),
            status=data.get("status", "failure"),
            reply=data.get("reply"),
            error=data.get("error"),
        )


@dataclass
class Task:
    """A scheduled task managed by the Scheduler.

    Attributes:
        task_id: unique identifier, e.g. ``task_001``.
        content: the user message text handed to ``run_agent_turn``.
        trigger_type: ``"once"`` / ``"interval"`` / ``"daily"``.
        trigger_rule: ISO datetime for ``once``, seconds (int) for
            ``interval``, ``"HH:MM"`` time string for ``daily``.
        next_run_at: ISO datetime of the next scheduled execution.
        status: ``"waiting"`` / ``"running"`` / ``"completed"`` /
            ``"cancelled"`` / ``"failed"``.
        session_id: the session the task runs in.
        execution_history: list of past ``TaskExecution`` records.
        created_at / updated_at: ISO timestamps.
    """

    task_id: str
    content: str
    trigger_type: str
    trigger_rule: str
    next_run_at: str
    status: str
    session_id: str
    execution_history: list = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return {
            "id": self.task_id,
            "content": self.content,
            "triggerType": self.trigger_type,
            "triggerRule": self.trigger_rule,
            "nextRunAt": self.next_run_at,
            "status": self.status,
            "sessionId": self.session_id,
            "executionHistory": [h.to_dict() for h in self.execution_history],
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        history_raw = data.get("executionHistory", [])
        if not isinstance(history_raw, list):
            history_raw = []
        return cls(
            task_id=data["id"],
            content=data.get("content", ""),
            trigger_type=data.get("triggerType", "once"),
            trigger_rule=data.get("triggerRule", ""),
            next_run_at=data.get("nextRunAt", _now_iso()),
            status=data.get("status", "waiting"),
            session_id=data.get("sessionId", "default"),
            execution_history=[TaskExecution.from_dict(h) for h in history_raw],
            created_at=data.get("createdAt", _now_iso()),
            updated_at=data.get("updatedAt", _now_iso()),
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TasksStoreError(RuntimeError):
    """Raised for user-facing task storage failures."""


class TasksStore:
    """Thread-safe CRUD store for scheduled tasks."""

    def __init__(self, tasks_file: Path):
        self._tasks_file = tasks_file
        self._tasks: list[Task] = []
        self._lock = threading.Lock()
        self.load_warning: str | None = None
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._tasks_file.exists():
            return
        try:
            raw = self._tasks_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("tasks 文件格式错误：顶层结构应为数组")
            self._tasks = [Task.from_dict(item) for item in data]
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
                json.dumps([t.to_dict() for t in self._tasks], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._tasks_file)
        except OSError as exc:
            raise TasksStoreError(f"保存 tasks 失败: {exc}") from exc

    # -- CRUD (thread-safe) --------------------------------------------------

    def create(
        self,
        content: str,
        trigger_type: str,
        trigger_rule: str,
        session_id: str,
    ) -> Task:
        with self._lock:
            now = _now_iso()
            next_run = self._calc_next_run(trigger_type, trigger_rule, now)
            task = Task(
                task_id=self._generate_id(),
                content=content,
                trigger_type=trigger_type,
                trigger_rule=str(trigger_rule),
                next_run_at=next_run,
                status="waiting",
                session_id=session_id,
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
        """Return waiting tasks whose ``next_run_at`` is <= *now_iso*."""
        with self._lock:
            due = [
                t for t in self._tasks
                if t.status == "waiting" and t.next_run_at <= now_iso
            ]
            return due

    def mark_running(self, task_id: str) -> None:
        self._update(task_id, status="running")

    def record_success(self, task_id: str, reply: str) -> None:
        now = _now_iso()
        with self._lock:
            task = self._get_locked(task_id)
            task.execution_history.append(
                TaskExecution(executed_at=now, status="success", reply=reply)
            )
            # Cap history
            if len(task.execution_history) > MAX_EXECUTION_HISTORY:
                task.execution_history = task.execution_history[-MAX_EXECUTION_HISTORY:]
            if task.trigger_type == "once":
                task.status = "completed"
                task.next_run_at = ""
            else:
                task.status = "waiting"
                task.next_run_at = self._calc_next_run(
                    task.trigger_type, task.trigger_rule, now
                )
            task.updated_at = now
            self._save()

    def record_failure(self, task_id: str, error: str) -> None:
        now = _now_iso()
        with self._lock:
            task = self._get_locked(task_id)
            task.execution_history.append(
                TaskExecution(executed_at=now, status="failure", error=error)
            )
            if len(task.execution_history) > MAX_EXECUTION_HISTORY:
                task.execution_history = task.execution_history[-MAX_EXECUTION_HISTORY:]
            # For periodic tasks, continue scheduling the next run
            if task.trigger_type == "once":
                task.status = "failed"
                task.next_run_at = ""
            else:
                task.status = "waiting"
                task.next_run_at = self._calc_next_run(
                    task.trigger_type, task.trigger_rule, now
                )
            task.updated_at = now
            self._save()

    def cancel(self, task_id: str) -> None:
        self._update(task_id, status="cancelled", next_run_at="")

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._get_locked(task_id)  # raises if not found
            self._tasks = [t for t in self._tasks if t.task_id != task_id]
            self._save()

    # -- internal helpers ----------------------------------------------------

    def _update(self, task_id: str, **kwargs) -> None:
        with self._lock:
            task = self._get_locked(task_id)
            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.updated_at = _now_iso()
            self._save()

    def _get_locked(self, task_id: str) -> Task:
        """Get task — caller must hold ``self._lock``."""
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

    @staticmethod
    def _calc_next_run(trigger_type: str, trigger_rule: str, from_iso: str) -> str:
        """Calculate the next run timestamp from *from_iso*.

        Boundary strategies (documented here for transparency):

        - **once**: returns *trigger_rule* unchanged (the target time).
        - **interval**: ``next = from_iso + interval_seconds``. If
          execution takes longer than the interval, the next run will
          fire immediately (already past due).
        - **daily**: next occurrence of ``HH:MM`` after *from_iso*. If
          the time has already passed today, returns tomorrow's.

        On restart, any waiting task whose ``next_run_at`` is in the
        past will be picked up by the scheduler on its next poll cycle
        and executed immediately.
        """
        try:
            dt = datetime.fromisoformat(from_iso)
        except ValueError:
            dt = datetime.now(timezone.utc)

        if trigger_type == "once":
            return str(trigger_rule)

        if trigger_type == "interval":
            try:
                secs = int(trigger_rule)
            except ValueError:
                secs = 3600
            return (dt + _ts_delta(seconds=secs)).isoformat(timespec="seconds")

        if trigger_type == "daily":
            try:
                parts = str(trigger_rule).strip().split(":")
                hour, minute = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                hour, minute = 9, 0
            candidate = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= dt:
                candidate = candidate.replace(day=candidate.day + 1)
            return candidate.isoformat(timespec="seconds")

        # Fallback: 1 hour from now
        return (dt + _ts_delta(seconds=3600)).isoformat(timespec="seconds")


# Helper: timezone-aware timedelta (Python <3.12 compat)
def _ts_delta(**kwargs) -> object:
    from datetime import timedelta
    return timedelta(**kwargs)
