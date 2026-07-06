"""Gateway HTTP server.

A FastAPI server that exposes the claw agent runtime to frontends
(web, desktop, chat-bot, …). It wraps ``run_agent_turn`` and never
calls ``LLMClient`` directly — every conversation flows through the
single agent entry point.

Session strategy for ``POST /chat``:
    - If ``sessionId`` is provided and exists → use that session.
    - If ``sessionId`` is missing → auto-create a new session.
    - If ``sessionId`` is provided but does **not** exist → return 404.

Start the server::

    python -m claw.gateway

Environment variables for the server (optional):
    GATEWAY_HOST  – bind address (default: 127.0.0.1)
    GATEWAY_PORT  – bind port (default: 8000)
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from claw.agent.loop import run_agent_turn
from claw.config import (
    DATA_DIR,
    MEMORY_FILE,
    PROJECT_ROOT,
    SESSIONS_DIR,
    ConfigError,
    load_config,
)
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.memory.store import MemoryStore
from claw.prompts import PromptLoadError, load_soul, load_system_prompt
from claw.session.store import SessionNotFoundError, SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry
from claw.tools.readonly import register_all_readonly
from claw.scheduler.tasks_store import TasksStore, TasksStoreError
from claw.scheduler.scheduler import Scheduler

# ---------------------------------------------------------------------------
# Shared runtime (initialised once at startup)
# ---------------------------------------------------------------------------

_config = load_config()
_system_prompt = load_system_prompt()
_soul = load_soul()

_llm_client = LLMClient(_config)
_session_store = SessionStore(SESSIONS_DIR)
_memory_store = MemoryStore(MEMORY_FILE)
_context_builder = ContextBuilder(_system_prompt, _soul, _memory_store)

_tool_registry = ToolRegistry()
register_all_readonly(_tool_registry)

_tasks_file = DATA_DIR / "tasks" / "tasks.json"
_tasks_store = TasksStore(_tasks_file)
_scheduler = Scheduler(
    _tasks_store, _session_store, _context_builder, _tool_registry, _llm_client
)

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Start scheduler on server boot
    if _tasks_store.load_warning:
        print(f"[scheduler] {_tasks_store.load_warning}")
    _scheduler.start()
    yield
    # Stop scheduler on server shutdown
    _scheduler.stop()


app = FastAPI(
    title="SJTUClaw Gateway",
    description="HTTP API for the claw agent runtime.",
    version="0.2.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        alias="sessionId",
        description="Target session id. If omitted, a new session is created.",
    )
    message: str = Field(..., min_length=1, description="User message text.")


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, description="Optional session title.")


class SessionInfo(BaseModel):
    session_id: str = Field(alias="sessionId")
    title: str
    message_count: int = Field(alias="messageCount")
    updated_at: str = Field(alias="updatedAt")

    model_config = {"populate_by_name": True}


class MessageInfo(BaseModel):
    role: str
    content: str


class AttachmentInfo(BaseModel):
    id: str
    original_name: str = Field(alias="originalName")
    stored_name: str = Field(alias="storedName")
    size: int
    mime_type: str = Field(alias="mimeType")
    uploaded_at: str = Field(alias="uploadedAt")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Attachment helpers (session-scoped metadata)
# ---------------------------------------------------------------------------


def _attachments_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id / "attachments"


def _meta_file(session_id: str) -> Path:
    return _attachments_dir(session_id) / ".meta.json"


def _read_attachments_meta(session_id: str) -> list[dict[str, Any]]:
    """Read attachment metadata list, returning [] on any error."""
    mf = _meta_file(session_id)
    if not mf.exists():
        return []
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_attachments_meta(session_id: str, meta: list[dict[str, Any]]) -> None:
    d = _attachments_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".meta.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_meta_file(session_id))


def _add_attachment_record(
    session_id: str,
    attachment_id: str,
    original_name: str,
    stored_name: str,
    size: int,
    mime_type: str,
) -> dict[str, Any]:
    record = {
        "id": attachment_id,
        "originalName": original_name,
        "storedName": stored_name,
        "size": size,
        "mimeType": mime_type,
        "uploadedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    meta = _read_attachments_meta(session_id)
    meta.append(record)
    _write_attachments_meta(session_id, meta)
    return record


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@app.exception_handler(LLMError)
async def _llm_error_handler(_request, exc: LLMError):
    return JSONResponse(
        status_code=502,
        content={"ok": False, "error": f"LLM 调用失败: {exc}"},
    )


@app.exception_handler(SessionStoreError)
async def _session_error_handler(_request, exc: SessionStoreError):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": f"Session 存储错误: {exc}"},
    )


@app.exception_handler(Exception)
async def _generic_error_handler(_request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": f"服务器内部错误: {exc}"},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/chat")
def handle_chat(req: ChatRequest):
    """Send a user message and get the assistant's reply.

    If ``sessionId`` is omitted a new session is created automatically
    and its id is returned in the response.  If ``sessionId`` is given
    but does not exist, a **404** is returned.
    """
    # Resolve session
    if req.session_id:
        if not _session_store.exists(req.session_id):
            raise HTTPException(
                status_code=404,
                detail=f"Session 不存在: {req.session_id}",
            )
        sid = req.session_id
    else:
        session = _session_store.create_session()
        sid = session.session_id

    # Run agent turn — this is the ONLY call site that invokes the LLM
    reply = run_agent_turn(
        sid,
        req.message,
        session_store=_session_store,
        context_builder=_context_builder,
        tool_registry=_tool_registry,
        llm_client=_llm_client,
    )

    # Return updated messages so the frontend can refresh
    session = _session_store.get(sid)
    messages = [{"role": m.role, "content": m.content} for m in session.messages]

    return {
        "ok": True,
        "type": "chat",
        "sessionId": sid,
        "reply": reply,
        "messages": messages,
    }


# ---------------------------------------------------------------------------
# Command endpoint — handles /session, /memory, /compact for web frontend
# ---------------------------------------------------------------------------

_COMMAND_PREFIXES = ("/session", "/memory", "/compact", "/exit", "/task")


class CommandRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        alias="sessionId",
        description="Current session id shown in the frontend.",
    )
    command: str = Field(..., min_length=1)


@app.post("/command")
def handle_command(req: CommandRequest):
    """Execute a CLI-style command and return the result.

    Supported commands are the same as the terminal CLI:
    ``/session new|list|switch|rename|delete``,
    ``/memory add|list|delete``, ``/compact``, ``/exit``.

    The response includes an ``actions`` list so the frontend knows
    which UI updates to perform (e.g. reload the session list).
    """
    root, *args = req.command.split()
    actions: list[str] = []
    result_text = ""
    switch_to: str | None = None

    # ---- /session ----------------------------------------------------------
    if root == "/session":
        sub = args[0] if args else ""
        if sub == "new":
            session = _session_store.create_session()
            result_text = f"Created session: {session.session_id}"
            actions = ["reload_sessions", "switch_session"]
            switch_to = session.session_id
        elif sub == "list":
            summaries = _session_store.list_summaries()
            if not summaries:
                result_text = "Sessions: (empty)"
            else:
                lines = ["Sessions:"]
                for s in summaries:
                    marker = "*" if s.session_id == req.session_id else " "
                    lines.append(
                        f"{marker} {s.session_id}  {s.title}  "
                        f"messages={s.message_count}  updated={s.updated_at}"
                    )
                result_text = "\n".join(lines)
            actions = ["reload_sessions"]
        elif sub == "switch":
            if not args[1:]:
                result_text = "用法: /session switch <sessionId>"
            else:
                sid = args[1]
                if not _session_store.exists(sid):
                    result_text = f"Session 不存在: {sid}"
                else:
                    result_text = f"Switched to session: {sid}"
                    actions = ["switch_session"]
                    switch_to = sid
        elif sub == "rename":
            if len(args) < 3:
                result_text = "用法: /session rename <sessionId> <title>"
            else:
                try:
                    _session_store.rename(args[1], " ".join(args[2:]))
                    result_text = f"Renamed session {args[1]}"
                    actions = ["reload_sessions"]
                except SessionStoreError as exc:
                    result_text = f"错误: {exc}"
        elif sub == "delete":
            if not args[1:]:
                result_text = "用法: /session delete <sessionId>"
            else:
                sid = args[1]
                if not _session_store.exists(sid):
                    result_text = f"Session 不存在: {sid}"
                else:
                    try:
                        _session_store.delete(sid)
                        result_text = f"Deleted session: {sid}"
                        actions = ["reload_sessions"]
                        if req.session_id == sid:
                            actions.append("switch_session")
                            summaries = _session_store.list_summaries()
                            switch_to = summaries[0].session_id if summaries else _session_store.ensure_default_session().session_id
                    except SessionStoreError as exc:
                        result_text = f"错误: {exc}"
        else:
            result_text = "用法: /session <new|list|switch|rename|delete> ..."

    # ---- /memory -----------------------------------------------------------
    elif root == "/memory":
        sub = args[0] if args else ""
        if sub == "add":
            prefix = "/memory add "
            if not req.command.startswith(prefix):
                result_text = "用法: /memory add <content>"
            else:
                content = req.command[len(prefix):].strip()
                if not content:
                    result_text = "用法: /memory add <content>"
                else:
                    try:
                        entry = _memory_store.add(content)
                        result_text = f"Added memory: {entry.memory_id}"
                    except Exception as exc:
                        result_text = f"错误: {exc}"
        elif sub == "list":
            entries = _memory_store.list()
            if not entries:
                result_text = "Memory: (empty)"
            else:
                result_text = "Memory:\n" + "\n".join(
                    f"  {e.memory_id}  {e.content}" for e in entries
                )
        elif sub == "delete":
            if not args[1:]:
                result_text = "用法: /memory delete <memoryId>"
            else:
                try:
                    _memory_store.delete(args[1])
                    result_text = f"Deleted memory: {args[1]}"
                except Exception as exc:
                    result_text = f"错误: {exc}"
        else:
            result_text = "用法: /memory <add|list|delete> ..."

    # ---- /compact ----------------------------------------------------------
    elif root == "/compact":
        if not req.session_id or not _session_store.exists(req.session_id):
            result_text = "没有当前 session，无法压缩"
        else:
            from claw.context.compaction import (
                KEEP_RECENT_MESSAGES,
                CompactionError,
                compact_and_persist,
            )
            session = _session_store.get(req.session_id)
            if len(session.messages) <= KEEP_RECENT_MESSAGES:
                result_text = (
                    f"当前 session 只有 {len(session.messages)} 条消息，"
                    f"不超过保留窗口（{KEEP_RECENT_MESSAGES}），无需压缩。"
                )
            else:
                try:
                    outcome = compact_and_persist(session, _session_store, _llm_client)
                    r = outcome.result
                    result_text = (
                        f"Compacted session {session.session_id}.\n"
                        f"Old messages: {r.old_message_count}\n"
                        f"Recent messages: {r.recent_message_count}\n"
                        f"Summary:\n{r.summary}"
                    )
                    actions = ["reload_messages"]
                except CompactionError as exc:
                    result_text = f"错误: {exc}"

    # ---- /task ---------------------------------------------------------------
    elif root in ("/task", "/tasks"):
        sub = args[0] if args else "list"
        if sub == "list":
            try:
                tasks = _tasks_store.list_all()
                if not tasks:
                    result_text = "暂无定时任务。在设置面板 > Tasks 中创建。"
                else:
                    lines = ["Tasks:"]
                    for t in tasks:
                        lines.append(
                            f"  {t.task_id} [{t.status}] {t.trigger_type} "
                            f"\"{t.content[:50]}\""
                        )
                    result_text = "\n".join(lines)
            except TasksStoreError as exc:
                result_text = f"错误: {exc}"
        elif sub == "cancel":
            if not args[1:]:
                result_text = "用法: /task cancel <taskId>"
            else:
                try:
                    task = _tasks_store.get(args[1])
                    if task.status not in ("waiting", "running"):
                        result_text = f"任务 {args[1]} 状态为 {task.status}，无法取消"
                    else:
                        _tasks_store.cancel(args[1])
                        result_text = f"已取消任务: {args[1]}"
                except TasksStoreError:
                    result_text = f"任务不存在: {args[1]}"
        elif sub == "delete":
            if not args[1:]:
                result_text = "用法: /task delete <taskId>"
            else:
                try:
                    task = _tasks_store.get(args[1])
                    if task.status == "running":
                        result_text = f"任务 {args[1]} 正在运行，请先取消"
                    else:
                        _tasks_store.delete(args[1])
                        result_text = f"已删除任务: {args[1]}"
                except TasksStoreError:
                    result_text = f"任务不存在: {args[1]}"
        else:
            result_text = "用法: /task <list|cancel|delete> ..."
        actions = ["reload_sessions"]

    # ---- /exit (web: just clear session state) ------------------------------
    elif root == "/exit":
        result_text = "bye."
        actions = ["clear_session"]

    else:
        result_text = f"未知命令: {root}"

    resp: dict = {
        "ok": True,
        "type": "command",
        "result": result_text,
        "actions": actions,
    }
    if switch_to:
        resp["switchToSessionId"] = switch_to
    return resp


@app.get("/sessions")
def list_sessions():
    """List all existing sessions."""
    summaries = _session_store.list_summaries()
    return {
        "ok": True,
        "sessions": [
            {
                "sessionId": s.session_id,
                "title": s.title,
                "messageCount": s.message_count,
                "updatedAt": s.updated_at,
            }
            for s in summaries
        ],
    }


@app.post("/sessions")
def create_session(req: CreateSessionRequest | None = None):
    """Create a new session."""
    title = req.title if req and req.title else None
    session = _session_store.create_session(title=title)
    return {
        "ok": True,
        "sessionId": session.session_id,
        "title": session.title,
    }


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    """Return the full message history for *session_id*."""
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    session = _session_store.get(session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "messages": [{"role": m.role, "content": m.content} for m in session.messages],
        "summary": session.summary,
    }


@app.post("/sessions/{session_id}/attachments")
async def upload_attachment(session_id: str, file: UploadFile = File(...)):
    """Upload an attachment to *session_id*.

    The file is stored under ``data/sessions/<sessionId>/attachments/``
    and is only visible to its owning session.
    """
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")

    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    original_name = file.filename
    content = await file.read()
    size = len(content)
    mime_type = file.content_type or "application/octet-stream"

    # Generate a collision-resistant stored name
    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    # Keep extension from original name for usability
    safe_suffix = Path(original_name).suffix
    if safe_suffix and len(safe_suffix) > 20:
        safe_suffix = ""
    stored_name = f"{attachment_id}{safe_suffix}"

    # Write file
    d = _attachments_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    file_path = d / stored_name
    file_path.write_bytes(content)

    # Record metadata
    record = _add_attachment_record(
        session_id, attachment_id, original_name, stored_name, size, mime_type
    )

    return {
        "ok": True,
        "attachment": {
            "id": record["id"],
            "originalName": record["originalName"],
            "storedName": record["storedName"],
            "size": record["size"],
            "mimeType": record["mimeType"],
            "uploadedAt": record["uploadedAt"],
        },
    }


@app.get("/sessions/{session_id}/attachments")
def list_attachments(session_id: str):
    """List attachment metadata for *session_id*."""
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    records = _read_attachments_meta(session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "attachments": [
            {
                "id": r["id"],
                "originalName": r.get("originalName", ""),
                "storedName": r.get("storedName", ""),
                "size": r.get("size", 0),
                "mimeType": r.get("mimeType", ""),
                "uploadedAt": r.get("uploadedAt", ""),
            }
            for r in records
        ],
    }


# ==========================================================================
# Admin routes — edit system prompt, soul, sessions, memory
# ==========================================================================

class UpdateContentRequest(BaseModel):
    content: str = Field(..., min_length=1)


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1)


# -- System prompt ----------------------------------------------------------


@app.get("/admin/system-prompt")
def get_system_prompt():
    """Return the current system prompt content."""
    return {"ok": True, "content": _system_prompt}


@app.put("/admin/system-prompt")
def update_system_prompt(req: UpdateContentRequest):
    """Overwrite the system prompt file and hot-reload it."""
    _write_prompt_file("system_prompt.md", req.content)
    nonlocal_sp = "_system_prompt"
    import claw.gateway.server as _srv
    _srv.__dict__[nonlocal_sp] = req.content
    _context_builder.update_system_prompt(req.content)
    return {"ok": True}


# -- Soul -------------------------------------------------------------------


@app.get("/admin/soul")
def get_soul():
    """Return the current soul content."""
    return {"ok": True, "content": _soul}


@app.put("/admin/soul")
def update_soul(req: UpdateContentRequest):
    """Overwrite the soul file and hot-reload it."""
    _write_prompt_file("soul.md", req.content)
    nonlocal_soul = "_soul"
    import claw.gateway.server as _srv
    _srv.__dict__[nonlocal_soul] = req.content
    _context_builder.update_soul(req.content)
    return {"ok": True}


# -- Session rename / delete ------------------------------------------------


@app.patch("/sessions/{session_id}")
def rename_session(session_id: str, req: RenameSessionRequest):
    """Rename a session."""
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    _session_store.rename(session_id, req.title)
    return {"ok": True}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """Delete a session and its attachments."""
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    import shutil
    _session_store.delete(session_id)
    att_dir = _attachments_dir(session_id)
    if att_dir.exists():
        shutil.rmtree(str(att_dir), ignore_errors=True)
    return {"ok": True}


# -- Memory management (RESTful wrappers) ------------------------------------


@app.get("/memories")
def list_memories():
    """List all memory entries."""
    entries = _memory_store.list()
    return {
        "ok": True,
        "memories": [
            {"id": e.memory_id, "content": e.content, "createdAt": e.created_at}
            for e in entries
        ],
    }


class AddMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1)


@app.post("/memories")
def add_memory(req: AddMemoryRequest):
    """Add a new memory entry."""
    entry = _memory_store.add(req.content)
    return {"ok": True, "id": entry.memory_id, "content": entry.content}


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    """Delete a memory entry."""
    _memory_store.delete(memory_id)
    return {"ok": True}


# -- Helpers ----------------------------------------------------------------


def _write_prompt_file(filename: str, content: str) -> None:
    """Write prompt content to ``prompts/<filename>`` atomically."""
    target = PROJECT_ROOT / "prompts" / filename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


# ==========================================================================
# Task management (Scheduler)
# ==========================================================================

from datetime import datetime, timezone


class CreateTaskRequest(BaseModel):
    content: str = Field(..., min_length=1, description="任务内容（到期后作为用户消息发送）")
    trigger_type: str = Field(..., alias="triggerType", description="once / interval / daily")
    trigger_rule: str = Field(..., alias="triggerRule", description="ISO时间 / 秒数 / HH:MM")
    session_id: str = Field(..., alias="sessionId", description="所属 session id")


@app.post("/tasks")
def create_task(req: CreateTaskRequest):
    """Create a scheduled task.

    *triggerType* must be one of ``once``, ``interval``, ``daily``.
    The session must exist.
    """
    if req.trigger_type not in ("once", "interval", "daily"):
        raise HTTPException(status_code=400, detail=f"无效的 triggerType: {req.trigger_type}")

    # Validate trigger_rule
    if req.trigger_type == "once":
        try:
            datetime.fromisoformat(req.trigger_rule)
        except ValueError:
            raise HTTPException(status_code=400, detail="once 任务的 triggerRule 必须是 ISO 时间格式")
    elif req.trigger_type == "interval":
        try:
            s = int(req.trigger_rule)
            if s < 10:
                raise HTTPException(status_code=400, detail="interval 至少为 10 秒")
        except ValueError:
            raise HTTPException(status_code=400, detail="interval 任务的 triggerRule 必须是整数秒数")
    elif req.trigger_type == "daily":
        try:
            parts = req.trigger_rule.strip().split(":")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="daily 任务的 triggerRule 格式必须为 HH:MM")

    if not _session_store.exists(req.session_id):
        # Auto-create the session for convenience
        _session_store.create_session(session_id=req.session_id)

    try:
        task = _tasks_store.create(
            content=req.content,
            trigger_type=req.trigger_type,
            trigger_rule=req.trigger_rule,
            session_id=req.session_id,
        )
    except TasksStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ok": True,
        "task": _task_to_dict(task),
    }


@app.get("/tasks")
def list_tasks():
    """List all tasks ordered by creation time (newest first)."""
    tasks = _tasks_store.list_all()
    return {
        "ok": True,
        "tasks": [_task_to_dict(t) for t in tasks],
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    """Get a single task including its full execution history."""
    try:
        task = _tasks_store.get(task_id)
    except TasksStoreError:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {
        "ok": True,
        "task": _task_to_dict(task),
    }


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    """Cancel a waiting task. Completed/cancelled tasks cannot be re-cancelled."""
    try:
        task = _tasks_store.get(task_id)
    except TasksStoreError:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status not in ("waiting", "running"):
        raise HTTPException(status_code=400, detail=f"任务状态为 {task.status}，无法取消")
    _tasks_store.cancel(task_id)
    return {"ok": True}


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    """Delete a task completely, including its execution history.

    Running tasks should be cancelled first.
    """
    try:
        task = _tasks_store.get(task_id)
    except TasksStoreError:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status == "running":
        raise HTTPException(status_code=400, detail="运行中的任务请先取消再删除")
    _tasks_store.delete(task_id)
    return {"ok": True}


def _task_to_dict(task) -> dict:
    return {
        "id": task.task_id,
        "content": task.content,
        "triggerType": task.trigger_type,
        "triggerRule": task.trigger_rule,
        "nextRunAt": task.next_run_at,
        "status": task.status,
        "sessionId": task.session_id,
        "executionHistory": [
            {
                "executedAt": h.executed_at,
                "status": h.status,
                "reply": h.reply,
                "error": h.error,
            }
            for h in task.execution_history
        ],
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
    }
