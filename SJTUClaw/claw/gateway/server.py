"""Gateway HTTP server.

A FastAPI server that exposes the claw agent runtime to frontends.

Start the server::

    python -m claw.gateway
"""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from claw.agent.loop import run_agent_turn
from claw.approval.manager import ApprovalManager, ApprovalRequest, ApprovalStatus
from claw.config import (
    DATA_DIR,
    MEMORY_FILE,
    PROJECT_ROOT,
    SESSIONS_DIR,
    load_config,
)
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.memory.store import MemoryStore
from claw.prompts import load_soul, load_system_prompt
from claw.session.store import SessionStore, SessionStoreError
from claw.skills.registry import SkillRegistry
from claw.tools.base import ToolRegistry
from claw.tools import register_all_tools
from claw.tools.download import get_download, list_downloads
from claw.workspace.manager import WorkspaceManager, WorkspaceError
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

_workspace_manager = WorkspaceManager()
_approval_manager = ApprovalManager()

# -- Skill registry (Step 9) -----------------------------------------------
_skill_registry = SkillRegistry()
_context_builder.set_skill_registry(_skill_registry)

_tool_registry = ToolRegistry()
_turn_session_id: str | None = None

register_all_tools(
    _tool_registry,
    workspace_manager=_workspace_manager,
    session_id_provider=lambda: _turn_session_id or "default",
    sessions_dir=SESSIONS_DIR,
    include_skill_tool=True,
)

_tasks_file = DATA_DIR / "tasks" / "tasks.json"
_tasks_store = TasksStore(_tasks_file)
_scheduler = Scheduler(
    _tasks_store, _session_store, _context_builder, _tool_registry, _llm_client
)


# -- Approval handler for Gateway (blocks on threading.Event) ----------------

def _gateway_approval_handler(req: ApprovalRequest) -> ApprovalRequest:
    """Block until the approval is decided via REST endpoint.

    The request is already registered; just wait for the event.
    """
    _approval_manager._requests[req.approval_id] = req
    _approval_manager._events[req.approval_id] = threading.Event()
    result = _approval_manager.wait(req.approval_id, timeout=300)
    return result or req


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if _tasks_store.load_warning:
        print(f"[scheduler] {_tasks_store.load_warning}")
    _scheduler.start()
    yield
    _scheduler.stop()


app = FastAPI(
    title="SJTUClaw Gateway",
    description="HTTP API for the claw agent runtime.",
    version="0.3.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Serve web UI at root ---------------------------------------------------
_WEB_DIR = PROJECT_ROOT / "web"


@app.get("/", response_class=HTMLResponse)
def serve_web_ui():
    """Serve the main web UI page."""
    index_path = _WEB_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Web UI not found")
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


# Mount web directory as static files (for future CSS/JS assets)
if _WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")


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
# Routes — Chat
# ---------------------------------------------------------------------------


@app.post("/chat")
def handle_chat(req: ChatRequest):
    """Send a user message and get the assistant's reply.

    If approval is required for a tool call, this endpoint will block
    until the approval is decided (or timeout).  The frontend should
    show a loading indicator during this time.
    """
    global _turn_session_id

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

    # Set thread-local session id for tool handlers
    _turn_session_id = sid

    try:
        reply = run_agent_turn(
            sid,
            req.message,
            session_store=_session_store,
            context_builder=_context_builder,
            tool_registry=_tool_registry,
            llm_client=_llm_client,
            approval_handler=_gateway_approval_handler,
            skill_registry=_skill_registry,
        )
    finally:
        _turn_session_id = None

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
# Routes — Command endpoint
# ---------------------------------------------------------------------------

_COMMAND_PREFIXES = (
    "/session", "/memory", "/compact", "/exit", "/task",
    "/workspace", "/approve", "/reject", "/approvals",
    "/skill",
)


class CommandRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        alias="sessionId",
        description="Current session id shown in the frontend.",
    )
    command: str = Field(..., min_length=1)


@app.post("/command")
def handle_command(req: CommandRequest):
    """Execute a CLI-style command and return the result."""
    root, *args = req.command.split()
    actions: list[str] = []
    result_text = ""
    switch_to: str | None = None
    sid = req.session_id or "default"

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
                target_sid = args[1]
                if not _session_store.exists(target_sid):
                    result_text = f"Session 不存在: {target_sid}"
                else:
                    result_text = f"Switched to session: {target_sid}"
                    actions = ["switch_session"]
                    switch_to = target_sid
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
                target_sid = args[1]
                if not _session_store.exists(target_sid):
                    result_text = f"Session 不存在: {target_sid}"
                else:
                    try:
                        _session_store.delete(target_sid)
                        result_text = f"Deleted session: {target_sid}"
                        actions = ["reload_sessions"]
                        if req.session_id == target_sid:
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

    # ---- /task -------------------------------------------------------------
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

    # ---- /workspace (Step 8) -----------------------------------------------
    elif root == "/workspace":
        sub = args[0] if args else ""
        if sub == "set":
            path_str = " ".join(args[1:]) if args[1:] else ""
            if not path_str:
                result_text = "用法: /workspace set <路径>"
            else:
                try:
                    resolved = _workspace_manager.set(sid, path_str)
                    result_text = f"Workspace 已设置为: {resolved}"
                    actions = ["reload_workspace"]
                except WorkspaceError as exc:
                    result_text = f"错误: {exc}"
        elif sub == "show":
            ws = _workspace_manager.get(sid)
            if ws is None:
                result_text = "当前 session 未设置 workspace。使用 /workspace set <路径> 来设置。"
            else:
                result_text = f"当前 workspace: {ws}"
            actions = ["reload_workspace"]
        elif sub == "unset":
            _workspace_manager.unset(sid)
            result_text = "Workspace 已取消设置。"
            actions = ["reload_workspace"]
        else:
            result_text = "用法: /workspace <set|show|unset> ..."

    # ---- /approvals --------------------------------------------------------
    elif root == "/approvals":
        pending = _approval_manager.get_pending()
        if not pending:
            result_text = "当前没有待审批的操作。"
        else:
            lines = ["待审批操作:"]
            for r in pending:
                args_safe = {
                    k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v)
                    for k, v in r.tool_args.items()
                }
                lines.append(
                    f"  [{r.approval_id}] {r.tool_name} session={r.session_id}"
                )
                lines.append(f"    参数: {args_safe}")
            lines.append("使用 /approve <id> 批准，/reject <id> [原因] 拒绝。")
            result_text = "\n".join(lines)

    # ---- /approve ----------------------------------------------------------
    elif root == "/approve":
        approval_id = args[0] if args else ""
        if not approval_id:
            pending = _approval_manager.get_pending()
            if len(pending) == 1:
                approval_id = pending[0].approval_id
            else:
                result_text = "用法: /approve <approvalId>"
        if approval_id:
            r = _approval_manager.approve(approval_id)
            if r is None:
                result_text = f"未找到审批请求: {approval_id}"
            else:
                result_text = f"已批准: [{r.approval_id}] {r.tool_name}"

    # ---- /reject -----------------------------------------------------------
    elif root == "/reject":
        approval_id = args[0] if args else ""
        if not approval_id:
            pending = _approval_manager.get_pending()
            if len(pending) == 1:
                approval_id = pending[0].approval_id
            else:
                result_text = "用法: /reject <approvalId> [原因]"
        if approval_id:
            reason = " ".join(args[1:]) if len(args) > 1 else ""
            r = _approval_manager.reject(approval_id, reason)
            if r is None:
                result_text = f"未找到审批请求: {approval_id}"
            else:
                reason_text = f"原因: {reason}" if reason else "未提供原因"
                result_text = f"已拒绝: [{r.approval_id}] {r.tool_name} ({reason_text})"

    # ---- /skill (Step 9) -------------------------------------------------
    elif root == "/skill":
        sub = args[0] if args else ""
        if sub == "list":
            skills = _skill_registry.list_skills()
            if not skills:
                result_text = "Skills: (empty)"
            else:
                lines = ["Skills:"]
                for s in skills:
                    lines.append(f"  {s.name}")
                    lines.append(f"    {s.description}")
                result_text = "\n".join(lines)
        elif sub == "show":
            if not args[1:]:
                result_text = "用法: /skill show <skill-name>"
            else:
                skill = _skill_registry.get_skill(args[1])
                if skill is None:
                    result_text = f"未找到 skill: \"{args[1]}\""
                else:
                    lines = [
                        f"Skill: {skill.name}",
                        f"描述: {skill.description}",
                        "",
                        "使用说明:",
                        skill.instructions[:1500],
                    ]
                    result_text = "\n".join(lines)
        elif sub == "usage":
            if not req.session_id:
                result_text = "没有当前 session"
            else:
                session = _session_store.get(req.session_id)
                records = session.skill_usage
                if not records:
                    result_text = "当前 session 暂无 skill 使用记录。"
                else:
                    lines = ["Skill 使用记录:"]
                    for i, r in enumerate(records, 1):
                        src = "显式调用" if r.get("source") == "explicit" else "模型自主"
                        lines.append(
                            f"  [{i}] {r.get('skillName','?')} | {src} | {r.get('usedAt','?')}"
                        )
                    result_text = "\n".join(lines)
        else:
            result_text = "用法: /skill <list|show|usage|<skill-name> <task>> ..."

    # ---- /exit (web: just clear session state) ----------------------------
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


# ---------------------------------------------------------------------------
# Routes — Sessions
# ---------------------------------------------------------------------------


@app.get("/sessions")
def list_sessions():
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
    title = req.title if req and req.title else None
    session = _session_store.create_session(title=title)
    return {
        "ok": True,
        "sessionId": session.session_id,
        "title": session.title,
    }


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str):
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    session = _session_store.get(session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "messages": [{"role": m.role, "content": m.content} for m in session.messages],
        "summary": session.summary,
    }


# ---------------------------------------------------------------------------
# Routes — Attachments
# ---------------------------------------------------------------------------


@app.post("/sessions/{session_id}/attachments")
async def upload_attachment(session_id: str, file: UploadFile = File(...)):
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")

    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    original_name = file.filename
    content = await file.read()
    size = len(content)
    mime_type = file.content_type or "application/octet-stream"

    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    safe_suffix = Path(original_name).suffix
    if safe_suffix and len(safe_suffix) > 20:
        safe_suffix = ""
    stored_name = f"{attachment_id}{safe_suffix}"

    d = _attachments_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    file_path = d / stored_name
    file_path.write_bytes(content)

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
# Step 8 Routes — Workspace, Approval, Download
# ==========================================================================


# -- Workspace ---------------------------------------------------------------

class SetWorkspaceRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    path: str = Field(..., min_length=1)


@app.get("/workspace")
def get_workspace(session_id: str = Query(..., alias="sessionId")):
    """Get the workspace for a session."""
    ws = _workspace_manager.get(session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "workspace": str(ws) if ws else None,
        "isSet": ws is not None,
    }


@app.post("/workspace")
def set_workspace(req: SetWorkspaceRequest):
    """Set the workspace for a session."""
    try:
        resolved = _workspace_manager.set(req.session_id, req.path)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "sessionId": req.session_id,
        "workspace": str(resolved),
    }


@app.delete("/workspace")
def unset_workspace(session_id: str = Query(..., alias="sessionId")):
    """Remove the workspace binding for a session."""
    _workspace_manager.unset(session_id)
    return {"ok": True, "sessionId": session_id}


# -- Approval ----------------------------------------------------------------

@app.get("/approvals")
def list_approvals(session_id: str | None = Query(None, alias="sessionId")):
    """List pending approvals, optionally filtered by session."""
    if session_id:
        all_reqs = _approval_manager.list_by_session(session_id)
        pending = [r for r in all_reqs if r.status == ApprovalStatus.PENDING.value]
    else:
        pending = _approval_manager.get_pending()
    return {
        "ok": True,
        "approvals": [r.to_dict() for r in pending],
    }


@app.post("/approvals/{approval_id}/approve")
def approve_request(approval_id: str):
    """Approve a pending approval request."""
    req = _approval_manager.approve(approval_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"审批请求不存在: {approval_id}")
    return {"ok": True, "approval": req.to_dict()}


class RejectRequest(BaseModel):
    reason: str = Field(default="", description="Optional rejection reason.")


@app.post("/approvals/{approval_id}/reject")
def reject_request(approval_id: str, body: RejectRequest | None = None):
    """Reject a pending approval request with an optional reason."""
    reason = body.reason if body else ""
    req = _approval_manager.reject(approval_id, reason)
    if req is None:
        raise HTTPException(status_code=404, detail=f"审批请求不存在: {approval_id}")
    return {"ok": True, "approval": req.to_dict()}


# -- Download ----------------------------------------------------------------

@app.get("/downloads")
def list_download_entries():
    """List all active download entries."""
    entries = list_downloads()
    return {
        "ok": True,
        "downloads": [
            {"downloadId": did, "fileName": fname}
            for did, fname in entries.items()
        ],
    }


@app.get("/downloads/{download_id}")
def serve_download(download_id: str):
    """Serve the file registered under *download_id*."""
    file_path = get_download(download_id)
    if file_path is None:
        raise HTTPException(
            status_code=404, detail=f"下载入口不存在或已过期: {download_id}"
        )
    if not file_path.exists():
        raise HTTPException(
            status_code=404, detail=f"文件已被删除: {file_path.name}"
        )
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


# ==========================================================================
# Step 9 Routes — Skills
# ==========================================================================


@app.get("/skills")
def list_skills_endpoint():
    """List all available skills (lightweight index)."""
    skills = _skill_registry.list_skills()
    return {
        "ok": True,
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "hasAssets": len(s.assets) > 0,
                "hasReferences": len(s.references) > 0,
            }
            for s in skills
        ],
    }


@app.get("/skills/{skill_name}")
def get_skill_detail(skill_name: str):
    """Get full details of a specific skill."""
    skill = _skill_registry.get_skill(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在: {skill_name}")
    return {
        "ok": True,
        "skill": {
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "assets": [str(a.relative_to(skill.directory)) for a in skill.assets],
            "references": [str(r.relative_to(skill.directory)) for r in skill.references],
        },
    }


@app.get("/sessions/{session_id}/skill-usage")
def get_skill_usage(session_id: str):
    """Get skill usage records for a session."""
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    session = _session_store.get(session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "records": session.skill_usage,
    }


# ==========================================================================
# Admin routes — edit system prompt, soul, sessions, memory
# ==========================================================================

class UpdateContentRequest(BaseModel):
    content: str = Field(..., min_length=1)


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1)


@app.get("/admin/system-prompt")
def get_system_prompt():
    return {"ok": True, "content": _system_prompt}


@app.put("/admin/system-prompt")
def update_system_prompt(req: UpdateContentRequest):
    _write_prompt_file("system_prompt.md", req.content)
    import claw.gateway.server as _srv
    _srv.__dict__["_system_prompt"] = req.content
    _context_builder.update_system_prompt(req.content)
    return {"ok": True}


@app.get("/admin/soul")
def get_soul():
    return {"ok": True, "content": _soul}


@app.put("/admin/soul")
def update_soul(req: UpdateContentRequest):
    _write_prompt_file("soul.md", req.content)
    import claw.gateway.server as _srv
    _srv.__dict__["_soul"] = req.content
    _context_builder.update_soul(req.content)
    return {"ok": True}


@app.patch("/sessions/{session_id}")
def rename_session(session_id: str, req: RenameSessionRequest):
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    _session_store.rename(session_id, req.title)
    return {"ok": True}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    _session_store.delete(session_id)
    att_dir = _attachments_dir(session_id)
    if att_dir.exists():
        shutil.rmtree(str(att_dir), ignore_errors=True)
    return {"ok": True}


@app.get("/memories")
def list_memories():
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
    entry = _memory_store.add(req.content)
    return {"ok": True, "id": entry.memory_id, "content": entry.content}


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    _memory_store.delete(memory_id)
    return {"ok": True}


def _write_prompt_file(filename: str, content: str) -> None:
    target = PROJECT_ROOT / "prompts" / filename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


# ==========================================================================
# Task management (Scheduler)
# ==========================================================================


class CreateTaskRequest(BaseModel):
    content: str = Field(..., min_length=1)
    trigger_type: str = Field(..., alias="triggerType")
    trigger_rule: str = Field(..., alias="triggerRule")
    session_id: str = Field(..., alias="sessionId")


@app.post("/tasks")
def create_task(req: CreateTaskRequest):
    if req.trigger_type not in ("once", "interval", "daily"):
        raise HTTPException(status_code=400, detail=f"无效的 triggerType: {req.trigger_type}")

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

    return {"ok": True, "task": _task_to_dict(task)}


@app.get("/tasks")
def list_tasks():
    tasks = _tasks_store.list_all()
    return {"ok": True, "tasks": [_task_to_dict(t) for t in tasks]}


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    try:
        task = _tasks_store.get(task_id)
    except TasksStoreError:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return {"ok": True, "task": _task_to_dict(task)}


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
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
