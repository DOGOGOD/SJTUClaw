"""Thread-safe projection of agent events into desktop-pet state."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _TaskState:
    session_id: str
    task: str
    phase: str = "thinking"
    message: str = "正在思考"
    animation: str = "running"
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class PetStateBroker:
    def __init__(self):
        self._lock = threading.RLock()
        self._tasks: dict[str, _TaskState] = {}

    def start_turn(self, session_id: str, task: str) -> None:
        with self._lock:
            self._tasks[session_id] = _TaskState(
                session_id=session_id,
                task=_compact(task, 72),
            )

    def handle_event(self, session_id: str, event: Any) -> None:
        with self._lock:
            state = self._tasks.get(session_id)
            if state is None:
                state = _TaskState(session_id=session_id, task="后台任务")
                self._tasks[session_id] = state
            state.updated_at = time.time()
            event_name = type(event).__name__
            if event_name == "ThinkingEvent":
                state.phase, state.message, state.animation = "thinking", "正在思考", "running"
            elif event_name == "ToolCallStartEvent":
                state.phase = "tool"
                state.message = f"正在执行：{_human_tool_name(event.tool_name)}"
                state.animation = "running"
            elif event_name == "ToolCallEndEvent":
                state.phase = "review" if event.ok else "failed"
                state.message = "正在检查结果" if event.ok else "执行遇到问题"
                state.animation = "review" if event.ok else "failed"
            elif event_name == "ErrorEvent":
                state.phase, state.message, state.animation = "failed", "任务遇到错误", "failed"
            elif event_name == "FinalEvent":
                state.phase, state.message, state.animation = "complete", "任务已完成", "review"
                state.finished_at = time.time()

    def approval_pending(self, session_id: str, tool_name: str) -> None:
        with self._lock:
            state = self._tasks.get(session_id) or _TaskState(session_id, "后台任务")
            state.phase = "waiting_approval"
            state.message = f"等待审批：{_human_tool_name(tool_name)}"
            state.animation = "waiting"
            state.updated_at = time.time()
            self._tasks[session_id] = state

    def approval_resolved(self, session_id: str, approved: bool) -> None:
        with self._lock:
            state = self._tasks.get(session_id)
            if state is None:
                return
            state.phase = "thinking" if approved else "review"
            state.message = "审批通过，继续执行" if approved else "已拒绝命令"
            state.animation = "running" if approved else "review"
            state.updated_at = time.time()

    def finish_turn(self, session_id: str, *, failed: bool = False) -> None:
        with self._lock:
            state = self._tasks.get(session_id)
            if state is None:
                state = _TaskState(session_id=session_id, task="后台任务")
                self._tasks[session_id] = state
            state.updated_at = time.time()
            state.finished_at = time.time()
            if failed:
                state.phase, state.message, state.animation = "failed", "任务遇到错误", "failed"
            else:
                state.phase, state.message, state.animation = "complete", "任务已完成", "review"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            stale = [
                sid for sid, task in self._tasks.items()
                if task.finished_at is not None and now - task.finished_at > 8
            ]
            for sid in stale:
                self._tasks.pop(sid, None)
            if not self._tasks:
                return {
                    "phase": "idle",
                    "message": "待命中",
                    "animation": "idle",
                    "task": "",
                    "sessionId": None,
                    "activeTaskCount": 0,
                }
            waiting = [t for t in self._tasks.values() if t.phase == "waiting_approval"]
            chosen = max(waiting or list(self._tasks.values()), key=lambda item: item.updated_at)
            return {
                "phase": chosen.phase,
                "message": chosen.message,
                "animation": chosen.animation,
                "task": chosen.task,
                "sessionId": chosen.session_id,
                "activeTaskCount": len(self._tasks),
            }


def _compact(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _human_tool_name(name: str) -> str:
    labels = {
        "shell": "终端命令",
        "write_file": "写入文件",
        "overwrite_file": "覆盖文件",
        "delete_file": "删除文件",
        "download": "下载文件",
        "web_search": "网络搜索",
    }
    return labels.get(name, name.replace("_", " "))
