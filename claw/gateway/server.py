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
    load_dream_config,
    load_heartbeat_config,
    MEMORY_DIR,
    PROJECT_ROOT,
    SESSIONS_DIR,
    load_config,
    load_compaction_config,
)
from claw.context.builder import ContextBuilder
from claw.context.compaction_worker import CompactionWorker
from claw.llm.client import LLMClient, LLMError
from claw.memory.history_log import HistoryLog
from claw.memory.store import MemoryStore
from claw.prompts import load_soul, load_system_prompt
from claw.session.store import SessionStore, SessionStoreError
from claw.skills.registry import SkillRegistry
from claw.tools.base import ToolRegistry
from claw.tools import register_all_tools
from claw.tools.download import get_download, list_downloads
from claw.workspace.manager import WorkspaceManager, WorkspaceError
from claw.cron.service import CronService
from claw.cron.callbacks import (
    DreamCallback,
    HeartbeatCallback,
    make_dream_system_job,
    make_heartbeat_system_job,
)
from claw.memory.reflection import ReflectionManager

# ---------------------------------------------------------------------------
# Shared runtime (initialised once at startup)
# ---------------------------------------------------------------------------

_config = load_config()
_system_prompt = load_system_prompt()
_soul = load_soul()

_llm_client = LLMClient(_config)
_session_store = SessionStore(SESSIONS_DIR)
_memory_store = MemoryStore(MEMORY_DIR)
_compact_cfg = load_compaction_config()

# -- Cross-session history log
_history_log = HistoryLog(
    MEMORY_DIR,
    max_entries=_compact_cfg.max_history_entries,
)

_context_builder = ContextBuilder(
    _system_prompt,
    _soul,
    _memory_store,
    history_log=_history_log,
    workspace_path=str(PROJECT_ROOT),
)

_workspace_manager = WorkspaceManager()
_approval_manager = ApprovalManager()

# -- Skill registry (Step 9) -----------------------------------------------
_skill_registry = SkillRegistry()
_context_builder.set_skill_registry(_skill_registry)

_tool_registry = ToolRegistry()
_turn_session_local = threading.local()

def _get_turn_session_id() -> str:
    """Return the per-thread session id, falling back to 'default'."""
    return getattr(_turn_session_local, "session_id", None) or "default"

def _set_turn_session_id(sid: str | None) -> None:
    _turn_session_local.session_id = sid

register_all_tools(
    _tool_registry,
    workspace_manager=_workspace_manager,
    session_id_provider=_get_turn_session_id,
    sessions_dir=SESSIONS_DIR,
    include_skill_tool=True,
    include_memory_tools=True,
    memory_store=_memory_store,
)

# -- Cron service (nanobot-compatible) ----------------------------------
_cron_store_path = DATA_DIR / "cron" / "jobs.json"
_cron_service = CronService(_cron_store_path)

# -- Dream system job ---------------------------------------------------
_dream_cfg = load_dream_config()
_dream_cb: DreamCallback | None = None
if _dream_cfg.enabled:
    _dream_cb = DreamCallback(
        MEMORY_DIR, _history_log, _llm_client,
        workspace_root=SESSIONS_DIR.parent,
    )
    _cron_service.register_system_job(make_dream_system_job(_dream_cfg))
    print(f"[启动] Dream: {_dream_cfg.describe_schedule()}")

# -- Heartbeat system job -----------------------------------------------
_hb_cfg = load_heartbeat_config()
if _hb_cfg.enabled:
    _hb_cb = HeartbeatCallback(
        SESSIONS_DIR.parent,
        _session_store,
        _context_builder,
        _tool_registry,
        _llm_client,
        keep_recent_messages=_hb_cfg.keep_recent_messages,
    )
    _cron_service.register_system_job(make_heartbeat_system_job(_hb_cfg))
    print(f"[启动] Heartbeat: 每 {_hb_cfg.interval_s}s")

# Set up the dispatcher
_cron_service.on_job = _make_cron_dispatcher()

_reflection_mgr = ReflectionManager(
    DATA_DIR / "memory", _memory_store, _session_store, _llm_client
)

# -- Compaction worker (v3: with idle auto-compaction) --------------------
_compact_llm: LLMClient | None = None
if _compact_cfg.model:
    from claw.config import LLMConfig as LC
    _compact_llm_config = LC(
        api_key=_compact_cfg.api_key or _config.api_key,
        base_url=_compact_cfg.base_url or _config.base_url,
        model=_compact_cfg.model,
        context_window=_config.context_window,
        context_usage_ratio=_config.context_usage_ratio,
    )
    _compact_llm = LLMClient(_compact_llm_config)
_compaction_worker = CompactionWorker(
    _llm_client,
    _session_store,
    compact_llm=_compact_llm,
    config=_compact_cfg,
    idle_ttl_minutes=_compact_cfg.idle_ttl_minutes,
)


# -- Cron dispatcher ----------------------------------------------------------


def _make_cron_dispatcher():
    """Build a dispatcher that routes cron jobs to their callbacks."""

    async def dispatch(job) -> str | None:
        if job.name == "dream" and _dream_cb is not None:
            return await _dream_cb(job)
        if job.name == "heartbeat" and _hb_cfg.enabled:
            return await _hb_cb(job)

        # User-created agent_turn jobs
        if job.payload.kind == "agent_turn" and job.payload.message:
            try:
                sid = job.payload.session_key or "default"
                if not _session_store.exists(sid):
                    _session_store.create_session(session_id=sid)

                _cron_mark_active(sid)
                try:
                    reply = run_agent_turn(
                        sid,
                        f"[定时任务: {job.name}]\n\n{job.payload.message}",
                        session_store=_session_store,
                        context_builder=_context_builder,
                        tool_registry=_tool_registry,
                        llm_client=_llm_client,
                    )
                finally:
                    _cron_mark_idle(sid)

                return reply
            except Exception as e:
                return f"定时任务 '{job.name}' 执行失败: {e}"

        return None

    return dispatch


# Active session tracking for cron deferral
_active_cron_sessions: set[str] = set()


def _cron_mark_active(session_key: str) -> None:
    _active_cron_sessions.add(session_key)


def _cron_mark_idle(session_key: str) -> None:
    _active_cron_sessions.discard(session_key)


# -- Approval handler for Gateway (blocks on threading.Event) ----------------

def _gateway_approval_handler(req: ApprovalRequest) -> ApprovalRequest:
    """Block until the approval is decided via REST endpoint.

    Uses ApprovalManager's thread-safe create() to register the request.
    """
    registered = _approval_manager.create(req.session_id, req.tool_name, req.tool_args)
    result = _approval_manager.wait(registered.approval_id, timeout=300)
    return result or req


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    import asyncio as _asyncio

    loop = _asyncio.get_running_loop()
    _cron_service.start(loop=loop)
    _reflection_mgr.start()
    _compaction_worker.start_idle_compaction()
    yield
    _compaction_worker.stop_idle_compaction()
    _compaction_worker.wait(timeout=5.0)
    _reflection_mgr.stop()
    _cron_service.stop()


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

# Web UI is served at the end of the file (after all API routes).
_WEB_DIR = PROJECT_ROOT / "web"

# Per-session AUTO mode flag (in-memory, resets on restart)
_auto_mode: dict[str, bool] = {}


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
    """Return the attachments directory for a session.

    Validates *session_id* to prevent path-traversal attacks.
    """
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise HTTPException(status_code=400, detail="无效的 session ID")
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

    # Set per-thread session id for tool handlers
    _set_turn_session_id(sid)

    reply: str
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
            compaction_worker=_compaction_worker,
            auto_mode=_auto_mode.get(sid, False),
        )
    except Exception as exc:
        # Ensure a response is still returned even if the agent crashes
        print(f"[gateway] agent turn 异常: {exc}")
        reply = "抱歉，处理你的消息时发生了内部错误。请重试。"
    finally:
        _set_turn_session_id(None)

    # Safely retrieve session messages for the response
    messages: list[dict[str, str]] = []
    try:
        session = _session_store.get(sid)
        messages = [{"role": m.role, "content": m.content} for m in session.messages]
    except Exception as exc:
        print(f"[gateway] 获取 session 消息失败: {exc}")

    return {
        "ok": True,
        "type": "chat",
        "sessionId": sid,
        "reply": reply,
        "messages": messages,
        "autoMode": _auto_mode.get(sid, False),
    }


# ---------------------------------------------------------------------------
# Routes — Command endpoint
# ---------------------------------------------------------------------------

_COMMAND_PREFIXES = (
    "/session", "/memory", "/compact", "/exit", "/cron",
    "/workspace", "/approve", "/reject", "/approvals",
    "/skill", "/reflect", "/help", "/auto",
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
                KEEP_RECENT_MESSAGES_MIN,
                CompactionError,
                compact_and_persist,
            )
            session = _session_store.get(req.session_id)
            if len(session.messages) <= KEEP_RECENT_MESSAGES_MIN:
                result_text = (
                    f"当前 session 只有 {len(session.messages)} 条消息，"
                    f"不超过保留窗口（{KEEP_RECENT_MESSAGES_MIN}），无需压缩。"
                )
            else:
                try:
                    outcome = compact_and_persist(session, _session_store, _compact_llm or _llm_client)
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

        # ---- /cron -------------------------------------------------------------
    elif root in ("/cron", "/cron"):
        sub = args[0] if args else "list"
        if sub == "list":
            try:
                jobs = _cron_service.list_jobs(include_disabled=True)
                if not jobs:
                    result_text = "暂无定时作业。使用 cron 工具创建。"
                else:
                    lines = ["Cron 作业:"]
                    for j in jobs:
                        kind = j.payload.kind
                        protected = " [系统]" if kind == "system_event" else ""
                        enabled = "" if j.enabled else " [已禁用]"
                        lines.append(
                            f"  {j.id} {j.name}{protected}{enabled} "
                            f"schedule={j.schedule.kind}"
                        )
                        if j.state.last_run_at_ms:
                            lines.append(f"    上次: status={j.state.last_status}")
                        if j.state.next_run_at_ms:
                            lines.append(f"    下次: {j.state.next_run_at_ms}")
                    result_text = "\n".join(lines)
            except Exception as exc:
                result_text = f"错误: {exc}"
        elif sub == "disable":
            if not args[1:]:
                result_text = "用法: /cron disable <jobId>"
            else:
                job = _cron_service.enable_job(args[1], enabled=False)
                if job is None:
                    result_text = f"作业不存在: {args[1]}"
                else:
                    result_text = f"已禁用作业: {args[1]}"
        elif sub == "enable":
            if not args[1:]:
                result_text = "用法: /cron enable <jobId>"
            else:
                job = _cron_service.enable_job(args[1], enabled=True)
                if job is None:
                    result_text = f"作业不存在: {args[1]}"
                else:
                    result_text = f"已启用作业: {args[1]}"
        elif sub == "delete":
            if not args[1:]:
                result_text = "用法: /cron delete <jobId>"
            else:
                result = _cron_service.remove_job(args[1])
                if result == "removed":
                    result_text = f"已删除作业: {args[1]}"
                elif result == "protected":
                    result_text = f"无法删除受保护的系统作业: {args[1]}"
                else:
                    result_text = f"作业不存在: {args[1]}"
        elif sub == "status":
            status = _cron_service.status()
            result_text = (
                f"Cron 服务: {'运行中' if status['enabled'] else '已停止'}\n"
                f"作业数: {status['jobs']}"
            )
        else:
            result_text = "用法: /cron <list|status|disable|enable|delete> ..."
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
    # ---- /reflect -----------------------------------------------------------
    elif root == "/reflect":
        sub = args[0] if args else ""
        if sub == "status":
            config = _reflection_mgr.get_config()
            last_run = config.get("lastRunAt") or "从未"
            history = config.get("runHistory", [])
            last_result = ""
            if history:
                last = history[-1]
                last_result = (
                    f" | 上次: {last.get('runAt','?')} "
                    f"检查 {last.get('sessionsReviewed',0)} session "
                    f"提取 {last.get('factsExtracted',0)} 条"
                )
            result_text = (
                f"每日反思: {'✅ 启用' if config.get('enabled') else '❌ 禁用'} "
                f"| 时间: {config.get('time','?')} "
                f"| 上次执行: {last_run}{last_result}"
            )
        elif sub == "enable":
            _reflection_mgr.update_config(enabled=True)
            result_text = "✅ 每日记忆反思已启用。"
        elif sub == "disable":
            _reflection_mgr.update_config(enabled=False)
            result_text = "❌ 每日记忆反思已禁用。"
        elif sub == "time":
            if len(args) >= 2:
                _reflection_mgr.update_config(time=args[1])
                result_text = f"反思时间已设置为 {args[1]}。"
            else:
                result_text = "用法: /reflect time <HH:MM>"
        elif sub == "now":
            result = _reflection_mgr.run_now()
            if result.get("ok"):
                result_text = (
                    f"即时反思完成。检查了 {result.get('sessionsReviewed', 0)} session，"
                    f"提取了 {result.get('factsExtracted', 0)} 条记忆。"
                )
            else:
                result_text = f"反思失败: {result.get('error', '未知错误')}"
        else:
            result_text = "用法: /reflect <status|enable|disable|time|now>"

    elif root == "/exit":
        result_text = "bye."
        actions = ["clear_session"]

    elif root == "/auto":
        sub = args[0] if args else "toggle"
        current = _auto_mode.get(sid, False)
        if sub in ("on", "enable", "1"):
            _auto_mode[sid] = True
            result_text = "AUTO 模式已开启。Agent 在 workspace 内的写操作和 shell 命令将自动执行，无需逐一审批。"
        elif sub in ("off", "disable", "0"):
            _auto_mode[sid] = False
            result_text = "AUTO 模式已关闭。Agent 的写操作和 shell 命令恢复审批。"
        else:  # toggle
            _auto_mode[sid] = not current
            state_text = "开启" if _auto_mode[sid] else "关闭"
            result_text = f"AUTO 模式已{state_text}。"
        actions = ["reload_auto_mode"]

    elif root == "/help":
        result_text = (
            "SJTUClaw 可用指令：\n\n"
            "  /help                    显示此帮助信息\n"
            "  /session <sub> ...       会话管理\n"
            "    new                      创建新会话\n"
            "    list                     列出所有会话\n"
            "    switch <id>              切换到指定会话\n"
            "    rename <id> <title>      重命名会话\n"
            "    delete <id>              删除会话\n"
            "  /memory <sub> ...        长期记忆管理\n"
            "    add <内容>               添加记忆\n"
            "    list [--category <类别>] 列出记忆\n"
            "    search <关键词>          搜索记忆\n"
            "    delete <memoryId>        删除记忆\n"
            "    stats                    记忆统计\n"
            "  /compact                 手动压缩当前会话历史\n"
            "  /workspace <sub> ...     工作区路径管理\n"
            "    set <路径>               设置工作区路径\n"
            "    show                     查看当前工作区\n"
            "    unset                    取消工作区设置\n"
            "  /cron <sub> ...          定时作业管理 (nanobot 兼容)\n"
            "    list                     列出所有作业\n"
            "    status                   服务状态\n"
            "    delete <taskId>          删除已完成任务\n"
            "  /skill <sub> ...         Skill 管理\n"
            "    list                     列出可用 Skills\n"
            "    show <name>              查看 Skill 详情\n"
            "    usage                    查看使用记录\n"
            "    <name> <任务描述>        使用指定 Skill 执行任务\n"
            "  /approvals               查看待审批操作\n"
            "  /approve [approvalId]    批准操作\n"
            "  /reject [approvalId]     拒绝操作\n"
            "  /reflect <sub> ...       每日记忆反思\n"
            "    status                   查看反思状态\n"
            "    enable/disable           启用/禁用\n"
            "    time <HH:MM>             设置执行时间\n"
            "    now                      立即执行\n"
            "  /auto [on|off]          AUTO 模式：开启后 workspace 内操作自动批准\n"
            "  /exit                    退出当前会话\n\n"
            "在 Web UI 中也可以直接发送消息，Agent 会自动处理。"
        )

    else:
        result_text = f"未知命令: {root}（输入 /help 查看可用指令）"

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
def list_memories(category: str | None = Query(None)):
    """List memory entries, optionally filtered by category."""
    entries = _memory_store.list_by_category(category)
    return {
        "ok": True,
        "memories": [
            {
                "id": e.memory_id,
                "content": e.content,
                "category": e.category,
                "tags": e.tags,
                "importance": e.importance,
                "sourceSessionId": e.source_session_id,
                "createdAt": e.created_at,
                "updatedAt": e.updated_at,
                "lastRecalledAt": e.last_recalled_at or None,
                "recallCount": e.recall_count,
            }
            for e in entries
        ],
    }


@app.get("/memories/search")
def search_memories(q: str = Query(..., min_length=1)):
    """Search memory entries by keyword."""
    results = _memory_store.recall(query=q, limit=10)
    return {
        "ok": True,
        "query": q,
        "count": len(results),
        "memories": [
            {
                "id": e.memory_id,
                "content": e.content,
                "category": e.category,
                "tags": e.tags,
                "importance": e.importance,
                "lastRecalledAt": e.last_recalled_at or None,
                "recallCount": e.recall_count,
            }
            for e in results
        ],
    }


@app.get("/memories/stats")
def memory_stats():
    """Return memory count by category."""
    stats = _memory_store.stats()
    total = sum(stats.values())
    return {"ok": True, "total": total, "categories": stats}


class AddMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1)
    category: str = Field(default="general")
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=3, ge=1, le=5)
    source_session_id: str = Field(default="", alias="sourceSessionId")

    model_config = {"populate_by_name": True}


@app.post("/memories")
def add_memory(req: AddMemoryRequest):
    entry = _memory_store.add(
        content=req.content,
        category=req.category,
        tags=req.tags,
        importance=req.importance,
        source_session_id=req.source_session_id,
    )
    return {
        "ok": True,
        "id": entry.memory_id,
        "content": entry.content,
        "category": entry.category,
        "tags": entry.tags,
        "importance": entry.importance,
        "createdAt": entry.created_at,
    }


class UpdateMemoryRequest(BaseModel):
    content: str = Field(..., min_length=1)


@app.patch("/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateMemoryRequest):
    """Update a memory entry's content."""
    try:
        entry = _memory_store.update(memory_id, req.content)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "ok": True,
        "id": entry.memory_id,
        "content": entry.content,
        "updatedAt": entry.updated_at,
    }


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str):
    try:
        _memory_store.delete(memory_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


# ==========================================================================
# Reflection (daily memory summarisation)
# ==========================================================================


@app.get("/reflect/config")
def get_reflection_config():
    """Get daily reflection configuration."""
    return {"ok": True, "config": _reflection_mgr.get_config()}


class UpdateReflectionRequest(BaseModel):
    enabled: bool | None = Field(default=None)
    time: str | None = Field(default=None)


@app.put("/reflect/config")
def update_reflection_config(req: UpdateReflectionRequest):
    """Update daily reflection config (enabled / time)."""
    kwargs = {}
    if req.enabled is not None:
        kwargs["enabled"] = req.enabled
    if req.time is not None:
        kwargs["time"] = req.time
    config = _reflection_mgr.update_config(**kwargs)
    return {"ok": True, "config": config}


@app.post("/reflect/run")
def trigger_reflection():
    """Trigger reflection immediately."""
    result = _reflection_mgr.run_now()
    return result


def _write_prompt_file(filename: str, content: str) -> None:
    target = PROJECT_ROOT / "prompts" / filename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


# ==========================================================================
# Cron management (nanobot-compatible)
# ==========================================================================


class CreateCronJobRequest(BaseModel):
    name: str | None = None
    message: str = Field(..., min_length=1)
    every_seconds: int | None = Field(default=None, alias="everySeconds")
    cron_expr: str | None = Field(default=None, alias="cronExpr")
    tz: str | None = None
    at: str | None = None
    session_id: str = Field(default="default", alias="sessionId")


@app.post("/cron/jobs")
def create_cron_job(req: CreateCronJobRequest):
    """Create a new cron job."""
    from zoneinfo import ZoneInfo

    from claw.cron.types import CronSchedule

    # Build schedule
    delete_after = False
    if req.every_seconds:
        schedule = CronSchedule(kind="every", every_ms=req.every_seconds * 1000)
    elif req.cron_expr:
        effective_tz = req.tz or "UTC"
        try:
            ZoneInfo(effective_tz)
        except Exception:
            raise HTTPException(status_code=400, detail=f"未知时区: {effective_tz}")
        schedule = CronSchedule(kind="cron", expr=req.cron_expr, tz=effective_tz)
    elif req.at:
        try:
            dt = datetime.fromisoformat(req.at)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效的 ISO 日期时间格式: {req.at}",
            )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        at_ms = int(dt.timestamp() * 1000)
        schedule = CronSchedule(kind="at", at_ms=at_ms)
        delete_after = True
    else:
        raise HTTPException(
            status_code=400,
            detail="需要 everySeconds、cronExpr 或 at 之一",
        )

    if not _session_store.exists(req.session_id):
        _session_store.create_session(session_id=req.session_id)

    job = _cron_service.add_job(
        name=req.name or req.message[:30],
        schedule=schedule,
        message=req.message,
        delete_after_run=delete_after,
        session_key=req.session_id,
        origin_channel="websocket",
        origin_chat_id=req.session_id,
    )

    return {"ok": True, "job": _cron_job_to_dict(job)}


@app.get("/cron/jobs")
def list_cron_jobs():
    """List all cron jobs."""
    jobs = _cron_service.list_jobs(include_disabled=True)
    return {"ok": True, "jobs": [_cron_job_to_dict(j) for j in jobs]}


@app.get("/cron/jobs/{job_id}")
def get_cron_job(job_id: str):
    """Get a single cron job."""
    job = _cron_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"作业不存在: {job_id}")
    return {"ok": True, "job": _cron_job_to_dict(job)}


@app.delete("/cron/jobs/{job_id}")
def delete_cron_job(job_id: str):
    """Delete a cron job (unless protected)."""
    result = _cron_service.remove_job(job_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail=f"作业不存在: {job_id}")
    if result == "protected":
        raise HTTPException(
            status_code=403,
            detail="无法删除受保护的系统作业",
        )
    return {"ok": True}


@app.post("/cron/jobs/{job_id}/disable")
def disable_cron_job(job_id: str):
    """Disable a cron job."""
    job = _cron_service.enable_job(job_id, enabled=False)
    if job is None:
        raise HTTPException(status_code=404, detail=f"作业不存在: {job_id}")
    return {"ok": True, "job": _cron_job_to_dict(job)}


@app.post("/cron/jobs/{job_id}/enable")
def enable_cron_job(job_id: str):
    """Enable a cron job."""
    job = _cron_service.enable_job(job_id, enabled=True)
    if job is None:
        raise HTTPException(status_code=404, detail=f"作业不存在: {job_id}")
    return {"ok": True, "job": _cron_job_to_dict(job)}


@app.get("/cron/status")
def cron_status():
    """Get cron service status."""
    return {"ok": True, "status": _cron_service.status()}


def _cron_job_to_dict(job) -> dict:
    """Serialize a CronJob to a dict for the API."""
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {
            "kind": job.schedule.kind,
            "atMs": job.schedule.at_ms,
            "everyMs": job.schedule.every_ms,
            "expr": job.schedule.expr,
            "tz": job.schedule.tz,
        },
        "payload": {
            "kind": job.payload.kind,
            "message": job.payload.message,
            "deliver": job.payload.deliver,
            "channel": job.payload.channel,
            "to": job.payload.to,
            "channelMeta": job.payload.channel_meta,
            "sessionKey": job.payload.session_key,
            "originChannel": job.payload.origin_channel,
            "originChatId": job.payload.origin_chat_id,
            "originMetadata": job.payload.origin_metadata,
        },
        "state": {
            "nextRunAtMs": job.state.next_run_at_ms,
            "lastRunAtMs": job.state.last_run_at_ms,
            "lastStatus": job.state.last_status,
            "lastError": job.state.last_error,
            "runHistory": [
                {
                    "runAtMs": r.run_at_ms,
                    "status": r.status,
                    "durationMs": r.duration_ms,
                    "error": r.error,
                }
                for r in job.state.run_history
            ],
        },
        "createdAtMs": job.created_at_ms,
        "updatedAtMs": job.updated_at_ms,
    }


# ---------------------------------------------------------------------------
# Web UI — SPA static files (must be last so API routes take precedence)
# StaticFiles with html=True serves index.html as fallback for unknown paths.
# ---------------------------------------------------------------------------

if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="webui")
