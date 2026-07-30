"""In-process adapter between the Textual UI and the shared gateway runtime.

The gateway already owns the canonical implementations for agent turns,
commands, approvals, sessions, cron, workspace policy, and backend selection.
The TUI calls those same handlers directly so behavior cannot drift between
the web, CLI, and terminal interfaces.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSnapshot:
    session_id: str
    title: str
    backend: str
    model: str
    workspace: str
    auto_mode: bool
    sandbox_mode: bool
    unlimited_mode: bool


class LocalRuntime:
    """Thin facade over :mod:`claw.gateway.server`."""

    def __init__(self) -> None:
        from claw.gateway import server

        self.server = server
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        cron_started = False
        try:
            self.server._cron_service.start(loop=asyncio.get_running_loop())
            cron_started = True
            self.server._reflection_mgr.start()
        except Exception:
            if cron_started:
                try:
                    self.server._cron_service.stop()
                except Exception:
                    logger.exception(
                        "TUI 启动回滚时停止 Cron 服务失败"
                    )
            raise
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        try:
            cleanup_steps = (
                ("Cron 服务", self.server._cron_service.stop),
                ("记忆反思服务", self.server._reflection_mgr.stop),
                ("Sandbox", self.server._sandbox_manager.close_all),
            )
            for label, cleanup in cleanup_steps:
                try:
                    cleanup()
                except Exception:
                    logger.exception("TUI 关闭时清理 %s 失败", label)
        finally:
            self._started = False

    def ensure_session(self) -> str:
        return self.server._session_store.ensure_default_session().session_id

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "sessionId": item.session_id,
                "title": item.title,
                "messageCount": item.message_count,
                "updatedAt": item.updated_at,
                "preview": item.preview,
            }
            for item in self.server._session_store.list_summaries()
        ]

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        if not self.server._session_store.exists(session_id):
            return []
        session = self.server._session_store.get(session_id)
        return self.server._visible_messages(session)

    async def stream(
        self, session_id: str, message: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the canonical Gateway turn events without an HTTP hop."""
        request = self.server.ChatRequest(session_id=session_id, message=message)
        response = await self.server.handle_chat_stream(request)
        buffer = ""
        async for chunk in response.body_iterator:
            text = (
                chunk.decode("utf-8", errors="replace")
                if isinstance(chunk, bytes)
                else chunk
            )
            buffer += text
            while "\n\n" in buffer:
                packet, buffer = buffer.split("\n\n", 1)
                for line in packet.splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload:
                        yield json.loads(payload)

    async def command(self, session_id: str, command: str) -> dict[str, Any]:
        request = self.server.CommandRequest(session_id=session_id, command=command)
        return await self.server.handle_command(request)

    def stop(self, session_id: str) -> str:
        result = self.server.handle_stop(self.server.StopRequest(session_id=session_id))
        return str(result["message"])

    def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return [
            request.to_dict()
            for request in self.server._approval_manager.list_by_session(session_id)
            if request.status == "pending"
        ]

    def approve(self, approval_id: str) -> bool:
        return self.server._approval_manager.approve(approval_id) is not None

    def reject(self, approval_id: str, reason: str = "") -> bool:
        return self.server._approval_manager.reject(approval_id, reason) is not None

    def cron_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for job in self.server._cron_service.list_jobs(include_disabled=True):
            schedule = job.schedule
            if schedule.kind == "cron":
                schedule_text = f"{schedule.expr or '—'} · {schedule.tz or 'local'}"
            elif schedule.kind == "every":
                seconds = (schedule.every_ms or 0) // 1000
                schedule_text = f"每 {seconds}s"
            else:
                schedule_text = self._format_ms(schedule.at_ms)
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "enabled": job.enabled,
                    "system": job.payload.kind == "system_event",
                    "schedule": schedule_text,
                    "nextRun": self._format_ms(job.state.next_run_at_ms),
                    "lastStatus": job.state.last_status or "—",
                }
            )
        return jobs

    async def trigger_cron(self, job_id: str) -> bool:
        return await self.server._cron_service.run_job(job_id, force=True)

    def snapshot(self, session_id: str) -> RuntimeSnapshot:
        if not self.server._session_store.exists(session_id):
            session_id = self.ensure_session()
        session = self.server._session_store.get(session_id)
        workspace = self.server._workspace_manager.get(session_id)
        if workspace is None and self.server._session_sandbox_mode(session_id):
            workspace_text = "sandbox:/workspace"
        else:
            workspace_text = str(workspace or "未绑定")
        return RuntimeSnapshot(
            session_id=session_id,
            title=session.title,
            backend=self.server._session_backend(session_id),
            model=self.server._llm_client.config.model or "未配置",
            workspace=workspace_text,
            auto_mode=self.server._auto_mode.get(session_id, False),
            sandbox_mode=self.server._session_sandbox_mode(session_id),
            unlimited_mode=self.server._workspace_manager.is_unlimited(session_id),
        )

    @staticmethod
    def _format_ms(value: int | None) -> str:
        if not value:
            return "—"
        return datetime.fromtimestamp(value / 1000).astimezone().strftime("%m-%d %H:%M")
