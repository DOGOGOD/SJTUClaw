"""Gateway HTTP server.

A FastAPI server that exposes the claw agent runtime to frontends.

Start the server::

    python -m claw.gateway
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import queue
import shutil
import threading
import uuid
from io import BytesIO
from urllib.parse import unquote, urlparse
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from claw.agent.events import (
    ErrorEvent,
    FinalEvent,
    ThinkingEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEvent,
)
from claw.agent.loop import run_agent_turn
from claw.approval.manager import ApprovalManager, ApprovalRequest, ApprovalStatus
from claw.config import (
    ConfigError,
    LLMConfig,
    DATA_DIR,
    load_heartbeat_config,
    MEMORY_DIR,
    PROJECT_ROOT,
    SESSIONS_DIR,
    load_config,
    load_compaction_config,
    load_qq_config,
)
from claw.context.builder import ContextBuilder
from claw.context.compaction_worker import CompactionWorker
from claw.llm.client import LLMClient, LLMError
from claw.memory.store import MemoryStore
from claw.prompts import load_soul, load_system_prompt
from claw.session.store import SessionStore, SessionStoreError
from claw.session.title import auto_title_if_first_turn
from claw.skills.registry import SkillRegistry
from claw.tools.base import ToolRegistry
from claw.tools import register_all_tools
from claw.tools.download import get_download, list_downloads
from claw.workspace.manager import WorkspaceManager, WorkspaceError
from claw.scheduler.service import CronService
from claw.scheduler.session_turns import visible_session_messages
from claw.scheduler.callbacks import (
    HeartbeatCallback,
    make_heartbeat_system_job,
)
from claw.memory.reflection import ReflectionManager
from claw.pet.catalog import PetCatalog, PetCatalogError
from claw.pet.process import PetProcessManager
from claw.pet.state import PetStateBroker
from claw.runtime_settings import (
    load_runtime_settings_raw,
    replace_runtime_settings_raw,
    setting_value,
    update_runtime_settings,
)
from claw.utils import default_timezone_name
from claw.paths import prompts_dir, web_dir

# -- QQ channel support ---------------------------------------------------------
from claw.channels.qq import QQChannel, QQConfig
from claw.channels.base import OutboundMessage
from claw.channels.qq_interactions import QQInteraction, parse_approval_button_data

logger = logging.getLogger(__name__)

_LLM_NOT_CONFIGURED_MESSAGE = "LLM 未配置，请先在设置中的 LLM 设置里填写 Base_url、API Key 和模型名称。"


class RuntimeLLMClient:
    """Mutable LLM client holder so WebUI can start before LLM is configured."""

    def __init__(self) -> None:
        self._client: LLMClient | None = None
        self._config = LLMConfig(api_key="", base_url="https://api.openai.com/v1", model="")
        self.error_message = _LLM_NOT_CONFIGURED_MESSAGE

    @property
    def config(self) -> LLMConfig:
        return self._client.config if self._client is not None else self._config

    @property
    def configured(self) -> bool:
        cfg = self.config
        return self._client is not None and bool(cfg.api_key and cfg.base_url and cfg.model)

    def set_config(self, config: LLMConfig) -> None:
        self._client = LLMClient(config)
        self._config = config
        self.error_message = ""

    def clear(self, message: str | None = None) -> None:
        self._client = None
        self.error_message = message or _LLM_NOT_CONFIGURED_MESSAGE

    def _require_client(self) -> LLMClient:
        if self._client is None:
            raise LLMError(self.error_message or _LLM_NOT_CONFIGURED_MESSAGE)
        return self._client

    def chat(self, *args, **kwargs):
        return self._require_client().chat(*args, **kwargs)

    def chat_with_tools(self, *args, **kwargs):
        return self._require_client().chat_with_tools(*args, **kwargs)


def _load_initial_llm() -> tuple[LLMConfig, RuntimeLLMClient]:
    client = RuntimeLLMClient()
    try:
        config = load_config()
        client.set_config(config)
        return config, client
    except ConfigError as exc:
        client.clear(_LLM_NOT_CONFIGURED_MESSAGE)
        logger.warning("LLM 未配置，WebUI 将以设置模式启动: %s", exc)
        return client.config, client

# ---------------------------------------------------------------------------
# Shared runtime (initialised once at startup)
# ---------------------------------------------------------------------------

_config, _llm_client = _load_initial_llm()
_system_prompt = load_system_prompt()
_soul = load_soul()

_session_store = SessionStore(SESSIONS_DIR)
_memory_store = MemoryStore(MEMORY_DIR)
_compact_cfg = load_compaction_config()
_workspace_manager = WorkspaceManager()

_context_builder = ContextBuilder(
    _system_prompt,
    _soul,
    _memory_store,
    workspace_path=str(PROJECT_ROOT),
    timezone=default_timezone_name(),
    workspace_manager=_workspace_manager,
)

_approval_manager = ApprovalManager()
_pet_catalog = PetCatalog(DATA_DIR)
_pet_state = PetStateBroker()


def _pet_gateway_url() -> str:
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{os.getenv('GATEWAY_PORT', '8000')}"


_pet_process = PetProcessManager(_pet_gateway_url(), DATA_DIR)

# -- Skill registry -----------------------------------------------------
_skill_registry = SkillRegistry()
_context_builder.set_skill_registry(_skill_registry)

_tool_registry = ToolRegistry()
_turn_session_local = threading.local()

def _get_turn_session_id() -> str:
    """Return the per-thread session id, falling back to 'default'."""
    return getattr(_turn_session_local, "session_id", None) or "default"

def _set_turn_session_id(sid: str | None) -> None:
    _turn_session_local.session_id = sid


def _persist_agent_fallback(session_id: str, message: str) -> str:
    """Persist a user-visible reply when an agent worker exits unexpectedly."""
    try:
        session = _session_store.get(session_id)
        last = session.messages[-1] if session.messages else None
        if not (
            last is not None
            and getattr(last, "role", "") == "assistant"
            and getattr(last, "content", "") == message
        ):
            session.append_message("assistant", message)
            _session_store.save(session)
    except Exception:
        logger.exception("无法持久化 Agent 异常兜底消息: session=%s", session_id)
    return message


def _llm_missing_reply() -> str:
    return _LLM_NOT_CONFIGURED_MESSAGE


def _llm_ready() -> bool:
    return bool(getattr(_llm_client, "configured", False))

# -- Cron service (must be created before register_all_tools) --------------
_cron_store_path = DATA_DIR / "cron" / "jobs.json"
_cron_service = CronService(_cron_store_path)

register_all_tools(
    _tool_registry,
    workspace_manager=_workspace_manager,
    session_id_provider=_get_turn_session_id,
    sessions_dir=SESSIONS_DIR,
    include_skill_tool=True,
    skill_registry=_skill_registry,
    include_memory_tools=True,
    memory_store=_memory_store,
    include_cron_tool=True,
    cron_service=_cron_service,
    default_timezone=default_timezone_name(),
)

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

# -- Cron dispatcher ----------------------------------------------------------


def _update_cron_context(
    session_key: str, chat_id: str, channel: str = "gateway",
    metadata: dict | None = None,
) -> None:
    """Update the cron tool's session context for the current turn.

    Must be called before each ``run_agent_turn`` so that cron jobs
    created by the LLM are bound to the correct session and channel.

    *metadata* (e.g. ``{"chat_type": "group"}``) is persisted in the
    cron job's ``origin_metadata`` so delivery can route correctly.
    """
    cron_tool = _tool_registry.get_tool("cron")
    if cron_tool is not None and hasattr(cron_tool, "set_context"):
        cron_tool.set_context(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            metadata=metadata,
        )


# Active session tracking for cron deferral
_active_cron_sessions: set[str] = set()


def _resolve_qq_chat_type(chat_id: str) -> str:
    """Determine the QQ chat type (c2c/group/dm) for *chat_id*.

    Checks session metadata first, falls back to "c2c".
    """
    for summary in _session_store.list_summaries():
        try:
            sess = _session_store.get(summary.session_id)
            if sess.metadata.get("qq_chat_id") == chat_id:
                return sess.metadata.get("qq_chat_type", "c2c")
        except Exception:
            continue
    return "c2c"


async def _deliver_cron_reply(
    channel: str, chat_id: str, reply: str,
    metadata: dict | None = None,
) -> None:
    """Deliver a cron job's reply back to the origin channel."""
    print(f"[cron] deliver_reply channel={channel} chat_id={chat_id}")
    if channel == "qq":
        if _qq_channel is None:
            print("[cron] QQ channel 未初始化，无法投递回复")
            return
        try:
            from claw.channels.base import OutboundMessage
            msg_meta = dict(metadata or {})
            if "chat_type" not in msg_meta:
                msg_meta["chat_type"] = _resolve_qq_chat_type(chat_id)
            await _qq_channel.send(OutboundMessage(
                chat_id=chat_id,
                content=reply,
                metadata=msg_meta,
            ))
            print(f"[cron] QQ 回复已投递 chat_id={chat_id} chat_type={msg_meta.get('chat_type', 'c2c')}")
        except Exception as exc:
            import traceback
            print(f"[cron] QQ 投递失败: {exc}")
            traceback.print_exc()
    elif channel in ("cli", ""):
        print(f"[cron] CLI job 回复: {reply[:200]}")
    elif channel in ("gateway", "websocket", "webui"):
        # run_agent_turn already persists the assistant reply in this session.
        # Writing it again here produced duplicate WebUI messages.
        print(f"[cron] Gateway 回复已由 agent loop 写入 session {chat_id}")
    else:
        print(f"[cron] 未知 channel '{channel}'，仅日志: {reply[:200]}")


def _cron_mark_active(session_key: str) -> None:
    _active_cron_sessions.add(session_key)


def _cron_mark_idle(session_key: str) -> None:
    _active_cron_sessions.discard(session_key)


def _cron_turn_start(session_key: str, message: str) -> None:
    """定时任务开始时更新桌宠状态。"""
    _pet_state.start_turn(session_key, f"[定时任务] {message[:60]}")


def _cron_turn_finish(session_key: str, failed: bool, reply: str | None) -> None:
    """定时任务结束时让桌宠播放动画并显示气泡通知。

    有回复时在气泡中显示（桌宠端按100字阈值截断+展开/收起）；
    无回复时仅标记任务完成让桌宠恢复空闲。
    """
    if failed:
        _pet_state.notify(session_key, "定时任务执行失败", animation="failed")
        return
    if reply:
        _pet_state.notify(session_key, reply.strip(), animation="jumping")
    else:
        _pet_state.finish_turn(session_key)


# -- Set up the cron dispatcher ------------------------------------------------
from claw.scheduler.dispatcher import create_cron_dispatcher

_cron_service.on_job = create_cron_dispatcher(
    session_store=_session_store,
    context_builder=_context_builder,
    tool_registry=_tool_registry,
    llm_client=_llm_client,
    set_turn_session_id=_set_turn_session_id,
    update_cron_context=_update_cron_context,
    on_heartbeat=_hb_cb if _hb_cfg.enabled else None,
    on_deliver=_deliver_cron_reply,
    on_session_resolve=lambda sid, chat_id: _find_existing_qq_session(chat_id),
    on_turn_active=_cron_mark_active,
    on_turn_idle=_cron_mark_idle,
    on_turn_start=_cron_turn_start,
    on_turn_finish=_cron_turn_finish,
    event_handler=lambda sid, event: _pet_state.handle_event(sid, event),
)

_reflection_mgr = ReflectionManager(
    DATA_DIR / "memory", _memory_store, _session_store, _llm_client
)

# -- Compaction worker (v3: with idle auto-compaction) --------------------
_compact_llm: LLMClient | None = None
if _compact_cfg.model and (_compact_cfg.api_key or _llm_ready()):
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


# -- Approval handler for Gateway (blocks on threading.Event) ----------------

def _gateway_approval_handler(req: ApprovalRequest) -> ApprovalRequest:
    """Block until the approval is decided via REST endpoint.

    Uses ApprovalManager's thread-safe create() to register the request.
    """
    registered = _approval_manager.create(req.session_id, req.tool_name, req.tool_args)
    _pet_state.approval_pending(req.session_id, req.tool_name)
    result = _approval_manager.wait(registered.approval_id, timeout=300)
    decided = result or req
    _pet_state.approval_resolved(
        req.session_id, decided.status == ApprovalStatus.APPROVED.value
    )
    return decided


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _qq_channel, _qq_task

    loop = asyncio.get_running_loop()
    _cron_service.start(loop=loop)
    _reflection_mgr.start()
    _compaction_worker.start_idle_compaction()

    pet_settings = _pet_catalog.load_settings()
    can_show_desktop = os.name == "nt" or bool(os.getenv("DISPLAY"))
    if (
        pet_settings.enabled
        and pet_settings.launch_on_gateway_start
        and can_show_desktop
        and not os.getenv("PYTEST_CURRENT_TEST")
    ):
        try:
            _pet_process.start()
        except OSError:
            logger.exception("桌面宠物启动失败")

    # -- Start QQ channel if enabled -------------------------------------------
    _qq_cfg = load_qq_config()
    if _qq_cfg.enabled:
        if not _qq_cfg.app_id or not _qq_cfg.client_secret:
            print("[QQ] QQ_ENABLED=true 但 QQ_APP_ID / QQ_CLIENT_SECRET 未设置")
            print("[QQ] 请前往 https://q.qq.com 创建机器人，获取 AppID 和 AppSecret")
            print("[QQ] 写入 .env 后重启 gateway")
        else:
            _qq_channel = QQChannel(QQConfig(
                enabled=True,
                app_id=_qq_cfg.app_id,
                client_secret=_qq_cfg.client_secret,
                allow_from=list(_qq_cfg.allow_from),
                markdown_support=_qq_cfg.markdown_support,
                ack_message=_qq_cfg.ack_message,
            ))
            _qq_channel.set_message_handler(_qq_message_handler)
            _qq_channel.set_interaction_handler(_qq_interaction_handler)
            _qq_task = asyncio.create_task(_qq_channel.start())
            print(f"[QQ] QQ 机器人已启动 (AppID: {_qq_cfg.app_id[:8]}...)")

    yield

    # -- Stop QQ channel -------------------------------------------------------
    if _qq_channel is not None:
        await _qq_channel.stop()
        _qq_channel = None
    if _qq_task is not None and not _qq_task.done():
        _qq_task.cancel()
        with suppress(asyncio.CancelledError):
            await _qq_task
        _qq_task = None

    _compaction_worker.stop_idle_compaction()
    _compaction_worker.wait(timeout=5.0)
    _reflection_mgr.stop()
    _cron_service.stop()
    _pet_process.stop()


app = FastAPI(
    title="SJTUClaw Gateway",
    description="HTTP API for the claw agent runtime.",
    version="0.3.0",
    lifespan=_lifespan,
)

# -- Custom middleware ---------------------------------------------------------
from claw.gateway.middleware import (
    GatewaySecurityMiddleware,
    MAX_ATTACHMENT_BYTES,
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    RequestSizeMiddleware,
    allowed_gateway_origins,
)
from claw.gateway.uploads import UploadTooLargeError, save_upload_limited

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_gateway_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-SJTUClaw-Token"],
)
app.add_middleware(RequestSizeMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(GatewaySecurityMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# Web UI is served at the end of the file (after all API routes).
_WEB_DIR = web_dir()

# Per-session AUTO mode flag (in-memory, resets on restart)
_auto_mode: dict[str, bool] = {}

# Active agent turns — tracks running turns for cancellation.
# Key: session_id, Value: threading.Event (set when cancelled).
_active_turns: dict[str, threading.Event] = {}
_active_turns_lock = threading.Lock()


def _register_active_turn(session_id: str) -> threading.Event | None:
    """Register a turn, or return None when the session is already busy."""
    event = threading.Event()
    with _active_turns_lock:
        if session_id in _active_turns:
            return None
        _active_turns[session_id] = event
    return event


def _unregister_active_turn(
    session_id: str, event: threading.Event | None = None
) -> None:
    """Remove a turn; identity-aware callers cannot remove a newer turn."""
    with _active_turns_lock:
        if event is None or _active_turns.get(session_id) is event:
            _active_turns.pop(session_id, None)


def _cancel_active_turn(session_id: str) -> bool:
    """Cancel the active turn for *session_id*. Returns True if found."""
    with _active_turns_lock:
        event = _active_turns.get(session_id)
    if event is not None:
        event.set()
        return True
    return False


def _cancel_all_active_turns() -> int:
    """Cancel all active turns. Returns the count cancelled."""
    with _active_turns_lock:
        events = list(_active_turns.values())
        count = len(events)
        for e in events:
            e.set()
    return count

# QQ channel instance (started in lifespan if QQ_ENABLED=true)
_qq_channel: QQChannel | None = None
_qq_task: asyncio.Task | None = None
_qq_onboard_tasks: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# QQ message handler — bridges QQ messages into the agent loop
# ---------------------------------------------------------------------------

# Map external QQ chat_ids to clean sequential session IDs.
# Keyed by chat_id, value is the internal session_id.
_qq_session_map: dict[str, str] = {}


_INLINE_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"
})


def _decorate_download_reply(
    session_id: str,
    reply: str,
    downloads_before: set[str],
) -> str:
    """Add inline-image Markdown for image downloads created this turn."""
    image_links: list[str] = []
    for download_id in set(list_downloads()) - downloads_before:
        file_path = get_download(download_id)
        if file_path is None or file_path.suffix.lower() not in _INLINE_IMAGE_EXTENSIONS:
            continue
        marker = f"/downloads/{download_id}"
        if marker not in reply:
            image_links.append(f"![{file_path.name}]({marker})")
    if not image_links:
        return reply

    decorated = reply.rstrip() + "\n\n图片已生成：\n" + "\n".join(image_links)
    try:
        session = _session_store.get(session_id)
        for message in reversed(session.messages):
            if message.role == "assistant" and message.content == reply:
                message.content = decorated
                _session_store.save(session)
                break
    except Exception:
        logger.exception("无法将图片 Markdown 写回 session: %s", session_id)
    return decorated


def _find_existing_qq_session(chat_id: str) -> str | None:
    """Scan sessions on disk for a QQ session previously bound to *chat_id*.

    After a gateway restart the in-memory ``_qq_session_map`` is lost,
    but we store ``qq_chat_id`` in session metadata so we can recover.
    """
    # Legacy check: session stored directly under the raw chat_id hash
    if _session_store.exists(chat_id):
        return chat_id
    # Scan all sessions for matching QQ metadata
    for summary in _session_store.list_summaries():
        sid = summary.session_id
        try:
            sess = _session_store.get(sid)
            if sess.metadata.get("qq_chat_id") == chat_id:
                return sid
        except Exception:
            continue
    return None


async def _qq_interaction_handler(event: QQInteraction) -> None:
    """Resolve an approval button after binding it to its chat and operator."""
    parsed = parse_approval_button_data(event.button_data)
    if parsed is None or _qq_channel is None:
        return
    approval_id, decision = parsed
    req = _approval_manager.get(approval_id)
    if req is None or req.status != ApprovalStatus.PENDING.value:
        return
    try:
        session = _session_store.get(req.session_id)
    except Exception:
        return
    if (
        str(session.metadata.get("qq_chat_id", "")) != event.chat_id
        or str(session.metadata.get("qq_sender_id", "")) != event.operator_id
    ):
        logger.warning("Rejected QQ approval interaction from a different chat/operator")
        return
    if decision == "approve":
        _approval_manager.approve(approval_id)
        result_text = f"已批准：{req.tool_name}"
    else:
        _approval_manager.reject(approval_id, "用户通过 QQ 按钮拒绝")
        result_text = f"已拒绝：{req.tool_name}"
    await _qq_channel.send(
        OutboundMessage(
            chat_id=event.chat_id,
            content=result_text,
            metadata={"chat_type": event.chat_type},
        )
    )


async def _qq_message_handler(
    sender_id: str,
    chat_id: str,
    content: str,
    media: list[str] | None,
    metadata: dict[str, Any] | None,
) -> str | None:
    """Handle an inbound QQ message by running the agent turn.

    Each QQ chat (C2C or group) gets its own session using the
    standard ``session_NNN`` naming convention, consistent with
    local/CLI sessions.
    """
    import asyncio as _asyncio

    # Resolve or create a session ID for this chat
    session_key = _qq_session_map.get(chat_id)
    chat_type = (metadata or {}).get("chat_type", "c2c")
    if session_key is None:
        # Try to find an existing session for this chat_id on disk
        existing = _find_existing_qq_session(chat_id)
        if existing is not None:
            session_key = existing
        else:
            # Create a new session — auto-generates session_NNN format
            session = _session_store.create_session(
                title=f"QQ 对话"
            )
            session_key = session.session_id
            # Persist the QQ chat_id mapping so we can recover
            # after a gateway restart.
            session.metadata["qq_chat_id"] = chat_id
            session.metadata["qq_chat_type"] = chat_type
            _session_store.save(session)
        _qq_session_map[chat_id] = session_key

    session = _session_store.get(session_key)
    is_approval_command = content.strip().lower().startswith(("/approve", "/reject"))
    bound_sender = str(session.metadata.get("qq_sender_id", ""))
    if is_approval_command and bound_sender and bound_sender != sender_id:
        return "只有发起该操作的 QQ 用户可以审批。"

    # Bind approvals to the exact user who initiated the agent turn. This
    # matters in group chats where another member could otherwise approve it.
    session.metadata["qq_chat_id"] = chat_id
    session.metadata["qq_chat_type"] = chat_type
    if not is_approval_command:
        session.metadata["qq_sender_id"] = sender_id
    _session_store.save(session)

    # Update cron tool context (with correct channel and chat_type metadata)
    _update_cron_context(
        session_key, chat_id, channel="qq",
        metadata={"chat_type": chat_type},
    )

    # Route slash commands directly (bypass LLM) — these run
    # synchronously in the current thread, so set the session id here.
    if _is_slash_command(content):
        _set_turn_session_id(session_key)
        try:
            return _execute_slash_command(content, session_key)
        finally:
            _set_turn_session_id(None)

    if not _llm_ready():
        reply = _llm_missing_reply()
        try:
            session.append_message("user", content)
            session.append_message("assistant", reply)
            _session_store.save(session)
        except Exception:
            logger.exception("无法持久化 QQ LLM 未配置提示: session=%s", session_key)
        return reply

    cancel_event = _register_active_turn(session_key)
    if cancel_event is None:
        return "当前会话已有任务正在运行，请稍后再试。"

    loop = _asyncio.get_running_loop()
    downloads_before = set(list_downloads())

    def _qq_approval_handler(req: ApprovalRequest) -> ApprovalRequest:
        _approval_manager.register(req)
        _pet_state.approval_pending(req.session_id, req.tool_name)
        if _qq_channel is not None:
            future = _asyncio.run_coroutine_threadsafe(
                _qq_channel.send_approval(
                    chat_id,
                    chat_type,
                    req.approval_id,
                    req.tool_name,
                    req.tool_args,
                    (metadata or {}).get("message_id"),
                ),
                loop,
            )
            try:
                future.result(timeout=15)
            except Exception:
                logger.exception("QQ approval prompt could not be sent")
        result = _approval_manager.wait(req.approval_id, timeout=300) or req
        _pet_state.approval_resolved(
            req.session_id, result.status == ApprovalStatus.APPROVED.value
        )
        return result

    # Non-command: run agent turn in a thread. The thread-local
    # session_id must be set INSIDE the worker thread.
    def _run_qq_turn() -> str:
        _set_turn_session_id(session_key)
        try:
            return run_agent_turn(
                session_key,
                content,
                session_store=_session_store,
                context_builder=_context_builder,
                tool_registry=_tool_registry,
                llm_client=_llm_client,
                approval_handler=_qq_approval_handler,
                compaction_worker=_compaction_worker,
                auto_mode=_auto_mode.get(session_key, False),
                unlimited_mode=_workspace_manager.is_unlimited(session_key),
                cancel_event=cancel_event,
            )
        finally:
            _set_turn_session_id(None)

    try:
        reply = await _asyncio.to_thread(_run_qq_turn)
        reply = _decorate_download_reply(session_key, reply, downloads_before)

        if _qq_channel is not None:
            new_download_ids = set(list_downloads()) - downloads_before
            paths = [
                str(path)
                for download_id in new_download_ids
                if (path := get_download(download_id)) is not None
            ]
            _qq_channel.queue_outbound_media(chat_id, paths)

        # Auto-title for QQ sessions
        try:
            sess = _session_store.get(session_key)
            msgs = [{"role": m.role, "content": m.content} for m in sess.messages]
            auto_title_if_first_turn(session_key, msgs, _session_store, _llm_client)
        except Exception:
            pass

        return reply
    except Exception as exc:
        print(f"[qq] agent turn 异常: {exc}")
        return "抱歉，处理你的消息时发生了内部错误。请重试。"
    finally:
        _unregister_active_turn(session_key, cancel_event)


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

    model_config = {"populate_by_name": True}


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
async def handle_chat(req: ChatRequest):
    """Send a user message and get the assistant's reply.

    If approval is required for a tool call, this endpoint will block
    until the approval is decided (or timeout).  The frontend should
    show a loading indicator during this time.

    The agent turn runs in a background thread via ``asyncio.to_thread``
    so the event loop remains responsive to other requests (e.g. /stop).
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

    # Defense in depth: WebUI normally sends slash commands to /command, but
    # stale frontend bundles or alternate clients may post them to /chat.
    # Never forward a recognized command to the LLM.
    command = req.message.strip()
    if _is_slash_command(command):
        result_text = _execute_slash_command(command, sid)
        session = _session_store.get(sid)
        messages = visible_session_messages(session)
        messages.extend([
            {"role": "user", "content": command, "_command": True},
            {"role": "assistant", "content": result_text, "_command": True},
        ])
        return {
            "ok": True,
            "type": "command",
            "sessionId": sid,
            "reply": result_text,
            "messages": messages,
            "autoMode": _auto_mode.get(sid, False),
            "unlimitedMode": _workspace_manager.is_unlimited(sid),
            "title": session.title,
        }

    if not _llm_ready():
        reply = _llm_missing_reply()
        try:
            session = _session_store.get(sid)
            session.append_message("user", req.message)
            session.append_message("assistant", reply)
            _session_store.save(session)
            messages = visible_session_messages(session)
            title = session.title
        except Exception:
            messages = [
                {"role": "user", "content": req.message},
                {"role": "assistant", "content": reply},
            ]
            title = None
        return {
            "ok": True,
            "type": "config_required",
            "sessionId": sid,
            "reply": reply,
            "messages": messages,
            "autoMode": _auto_mode.get(sid, False),
            "unlimitedMode": _workspace_manager.is_unlimited(sid),
            "title": title,
        }

    downloads_before = set(list_downloads())

    # Register a cancel event so /stop can interrupt this turn
    cancel_event = _register_active_turn(sid)
    if cancel_event is None:
        raise HTTPException(status_code=409, detail="该 Session 已有任务正在运行，请等待完成或先停止任务。")

    reply: str
    _turn_failed = False
    try:
        # Wrap in a callable so _set_turn_session_id runs inside the worker
        # thread — threading.local() is not shared across threads.
        def _run() -> str:
            _set_turn_session_id(sid)
            _update_cron_context(sid, req.session_id or "default")
            _pet_state.start_turn(sid, req.message)
            try:
                return run_agent_turn(
                    sid,
                    req.message,
                    session_store=_session_store,
                    context_builder=_context_builder,
                    tool_registry=_tool_registry,
                    llm_client=_llm_client,
                    approval_handler=_gateway_approval_handler,
                    compaction_worker=_compaction_worker,
                    auto_mode=_auto_mode.get(sid, False),
                    unlimited_mode=_workspace_manager.is_unlimited(sid),
                    event_callback=lambda event: _pet_state.handle_event(sid, event),
                    cancel_event=cancel_event,
                )
            except Exception:
                _pet_state.finish_turn(sid, failed=True)
                raise
            finally:
                _set_turn_session_id(None)

        reply = await asyncio.to_thread(_run)
    except Exception as exc:
        # Ensure a response is still returned even if the agent crashes
        _turn_failed = True
        print(f"[gateway] agent turn 异常: {exc}")
        reply = _persist_agent_fallback(
            sid, "抱歉，Agent 在执行工具时意外中止。已保留现有进度，请重试。"
        )
    finally:
        _unregister_active_turn(sid, cancel_event)

    # 成功时标记桌宠任务完成。
    # 注意：不调用 _pet_state.notify 推送 reply，因为 /chat 的 reply 已通过
    # HTTP 响应返回给桌宠（_show_reply → _set_local_message），两条路径同时
    # 显示会导致消息重复。定时任务回复仍由 _cron_turn_finish 中的 notify 推送。
    if not _turn_failed:
        _pet_state.finish_turn(sid)

    reply = _decorate_download_reply(sid, reply, downloads_before)

    # Safely retrieve session messages for the response
    messages: list[dict[str, Any]] = []
    try:
        session = _session_store.get(sid)
        messages = visible_session_messages(session)
    except Exception as exc:
        print(f"[gateway] 获取 session 消息失败: {exc}")

    # Auto-title: generate a title from the user's first message.
    # Returns the new title (or None) so it can be included in the response
    # for instant frontend updates without waiting for session-list refresh.
    new_title = auto_title_if_first_turn(sid, messages, _session_store, _llm_client)

    # If auto-title didn't trigger (e.g. short message), return the current
    # session title so the frontend can still display it.
    if new_title is None:
        try:
            current_session = _session_store.get(sid)
            new_title = current_session.title
        except Exception:
            pass

    return {
        "ok": True,
        "type": "chat",
        "sessionId": sid,
        "reply": reply,
        "messages": messages,
        "autoMode": _auto_mode.get(sid, False),
        "unlimitedMode": _workspace_manager.is_unlimited(sid),
        "title": new_title,
    }


# ---------------------------------------------------------------------------
# Routes — Stop (cancel running agent turn)
# ---------------------------------------------------------------------------


class StopRequest(BaseModel):
    """Request body for the /stop endpoint."""

    session_id: str | None = None
    all: bool = False


@app.post("/stop")
def handle_stop(req: StopRequest):
    """Cancel a running agent turn.

    If ``all=True``, cancels all active turns across all sessions.
    If ``session_id`` is provided, cancels only that session's turn.
    Otherwise, cancels all active turns (same as ``all=True``).
    """
    if req.all or req.session_id is None:
        count = _cancel_all_active_turns()
        return {
            "ok": True,
            "cancelled": count,
            "message": f"已终止 {count} 个正在运行的任务" if count > 0 else "当前没有正在运行的任务",
        }

    found = _cancel_active_turn(req.session_id)
    return {
        "ok": True,
        "cancelled": 1 if found else 0,
        "message": f"已终止 session `{req.session_id}` 的任务" if found else f"session `{req.session_id}` 当前没有正在运行的任务",
    }


# ---------------------------------------------------------------------------
# Slash command handler (shared by HTTP /command and QQ)
# ---------------------------------------------------------------------------


def _is_slash_command(text: str) -> bool:
    """Check if *text* starts with a known slash command."""
    from claw.cli.commands import is_command
    return is_command(text)


def _execute_slash_command(command: str, session_id: str) -> str:
    """Execute a slash command via the shared CLI command handler.

    Constructs a RuntimeState from the module-level globals and delegates
    to ``commands.handle_command``.  Gateway-specific callbacks are wired
    in so that /stop and /exit work correctly.
    """
    from claw.cli.commands import RuntimeState, handle_command

    sid = session_id or "default"

    def _stop_impl() -> str:
        found = _cancel_active_turn(sid)
        if found:
            return "已终止当前任务"
        count = _cancel_all_active_turns()
        if count > 0:
            return f"已终止 {count} 个正在运行的任务"
        return "当前没有正在运行的任务"

    def _exit_impl() -> str:
        _auto_mode.pop(sid, None)
        _workspace_manager.set_unlimited(sid, False)
        return "bye."

    state = RuntimeState(
        session_store=_session_store,
        memory_store=_memory_store,
        llm_client=_llm_client,
        current_session_id=sid,
        workspace_manager=_workspace_manager,
        approval_manager=_approval_manager,
        reflection_manager=_reflection_mgr,
        cron_service=_cron_service,
        pet_catalog=_pet_catalog,
        pet_process=_pet_process,
        auto_mode=_auto_mode.get(sid, False),
        stop_handler=_stop_impl,
        exit_handler=_exit_impl,
    )
    result = handle_command(command, state, markdown=True)
    if state.auto_mode:
        _auto_mode[sid] = True
    else:
        _auto_mode.pop(sid, None)
    return result


# ---------------------------------------------------------------------------
# Routes — Command endpoint
# ---------------------------------------------------------------------------


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
    result_text = _execute_slash_command(req.command, req.session_id or "default")

    # Determine actions for WebUI
    root, *args = req.command.split()
    actions: list[str] = []
    switch_to: str | None = None
    sid = req.session_id or "default"

    if root == "/session" and args and args[0] == "new":
        actions = ["reload_sessions", "switch_session"]
        session = _session_store.list_summaries()
        if session:
            switch_to = session[0].session_id
    elif root == "/session" and args and args[0] in ("list", "rename", "delete"):
        actions = ["reload_sessions"]
    elif root in ("/workspace", "/cron"):
        actions = ["reload_sessions"]
    elif root == "/pet" and (not args or args[0] in ("settings", "config")):
        actions = ["open_pet_settings"]
    elif root == "/exit":
        actions = ["clear_session"]
    elif root == "/auto":
        actions = ["reload_auto_mode"]
    elif root == "/compact":
        actions = ["reload_messages"]
    elif root == "/unlimited":
        actions = ["reload_unlimited_mode"]

    resp: dict = {
        "ok": True,
        "type": "command",
        "format": "markdown",
        "result": result_text,
        "actions": actions,
        "autoMode": _auto_mode.get(sid, False),
        "unlimitedMode": _workspace_manager.is_unlimited(sid),
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
        "messages": visible_session_messages(session),
        "summary": session.summary,
        "autoMode": _auto_mode.get(session_id, False),
        "unlimitedMode": _workspace_manager.is_unlimited(session_id),
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
    mime_type = file.content_type or "application/octet-stream"

    attachment_id = f"att_{uuid.uuid4().hex[:12]}"
    safe_suffix = Path(original_name).suffix
    if safe_suffix and len(safe_suffix) > 20:
        safe_suffix = ""
    stored_name = f"{attachment_id}{safe_suffix}"

    d = _attachments_dir(session_id)
    file_path = d / stored_name
    try:
        size = await save_upload_limited(
            file, file_path, max_bytes=MAX_ATTACHMENT_BYTES
        )
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    record = _add_attachment_record(
        session_id, attachment_id, original_name, stored_name, size, mime_type
    )

    content_url = f"/sessions/{session_id}/attachments/{attachment_id}"
    markdown_name = original_name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    message_content = (
        f"![{markdown_name}]({content_url})"
        if mime_type.startswith("image/")
        else f"[{markdown_name}]({content_url})"
    )
    session = _session_store.get(session_id)
    session.append_message("user", message_content, _command=True)
    _session_store.save(session)

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
        "message": {"role": "user", "content": message_content, "command": True},
    }


@app.get("/sessions/{session_id}/attachments/{attachment_id}")
def get_attachment_content(session_id: str, attachment_id: str):
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    record = next(
        (r for r in _read_attachments_meta(session_id) if r.get("id") == attachment_id),
        None,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="附件不存在")
    root = _attachments_dir(session_id).resolve()
    path = (root / str(record.get("storedName", ""))).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的附件路径") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="附件文件不存在")
    media_type = str(record.get("mimeType") or "application/octet-stream")
    if media_type.startswith("image/"):
        return FileResponse(path, media_type=media_type, content_disposition_type="inline")
    return FileResponse(
        path,
        media_type=media_type,
        filename=str(record.get("originalName") or path.name),
    )


@app.get("/sessions/{session_id}/local-image")
def get_local_image(session_id: str, path: str = Query(...)):
    if not _session_store.exists(session_id):
        raise HTTPException(status_code=404, detail=f"Session 不存在: {session_id}")
    workspace = _workspace_manager.get(session_id)
    # Sessions without an explicit binding still operate from the project
    # workspace, so expose images there while retaining the same boundary.
    root = (workspace or PROJECT_ROOT).resolve()
    candidate_path = Path(path)
    candidate = (
        candidate_path.resolve()
        if candidate_path.is_absolute()
        else (root / candidate_path).resolve()
    )
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="图片路径超出 workspace") from exc
    media_type = mimetypes.guess_type(candidate.name)[0] or ""
    if not candidate.is_file() or not media_type.startswith("image/"):
        raise HTTPException(status_code=404, detail="本地图片不存在或格式不受支持")
    if candidate.stat().st_size > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片不能超过 20 MB")
    return FileResponse(candidate, media_type=media_type, content_disposition_type="inline")


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


def _enable_native_dialog_dpi_awareness() -> None:
    """Opt the process into native DPI scaling before opening Tk dialogs."""
    if os.name != "nt":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            # PER_MONITOR_AWARE_V2 avoids Windows bitmap-upscaling the dialog.
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()
    except (AttributeError, OSError, ValueError):
        return


def _pick_workspace_directory() -> str:
    """Open a native folder picker on the machine running the Gateway."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("当前 Python 环境不支持原生文件夹选择器") from exc

    try:
        _enable_native_dialog_dpi_awareness()
        root = tk.Tk()
    except Exception as exc:
        raise RuntimeError("当前 Gateway 无法打开图形文件夹选择器") from exc
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return str(filedialog.askdirectory(title="选择 SJTUClaw Workspace") or "")
    finally:
        root.destroy()


@app.post("/workspace/pick")
async def pick_workspace_directory():
    """Open a native folder picker without blocking the async Gateway loop."""
    try:
        path = await asyncio.to_thread(_pick_workspace_directory)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "cancelled": not bool(path), "path": path}


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
    raw_path = req.path.strip()
    if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] and raw_path[0] in {'"', "'"}:
        raw_path = raw_path[1:-1].strip()
    if raw_path.lower().startswith("file://"):
        parsed = urlparse(raw_path)
        raw_path = unquote(parsed.path)
        if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
            raw_path = raw_path[1:]
    if not raw_path:
        raise HTTPException(status_code=400, detail="workspace 路径不能为空")
    try:
        resolved = _workspace_manager.set(req.session_id, raw_path)
    except (WorkspaceError, OSError) as exc:
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


# -- Desktop pet -------------------------------------------------------------

class PetSettingsRequest(BaseModel):
    enabled: bool | None = None
    selected_pet_id: str | None = Field(default=None, alias="selectedPetId")
    launch_on_gateway_start: bool | None = Field(
        default=None, alias="launchOnGatewayStart"
    )


class PetPositionRequest(BaseModel):
    x: int
    y: int


def _public_pet(pet: dict[str, Any] | None) -> dict[str, Any] | None:
    if pet is None:
        return None
    result = {key: value for key, value in pet.items() if key != "spritesheetPath"}
    result["spritesheetUrl"] = f"/pet/pets/{pet['id']}/spritesheet"
    return result


@app.get("/pet/settings")
def get_pet_settings():
    settings = _pet_catalog.load_settings()
    return {
        "ok": True,
        "settings": settings.to_dict(),
        "running": _pet_process.running,
    }


@app.put("/pet/settings")
def update_pet_settings(req: PetSettingsRequest):
    before = _pet_catalog.load_settings()
    try:
        settings = _pet_catalog.update_settings(
            enabled=req.enabled,
            selected_pet_id=req.selected_pet_id,
            launch_on_gateway_start=req.launch_on_gateway_start,
        )
    except PetCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    selected_changed = settings.selected_pet_id != before.selected_pet_id
    if not settings.enabled:
        _pet_process.stop()
    elif selected_changed:
        _pet_process.stop()
        _pet_process.start()
    elif not _pet_process.running:
        _pet_process.start()
    return {
        "ok": True,
        "settings": settings.to_dict(),
        "running": _pet_process.running,
    }


@app.get("/pet/pets")
def list_pets():
    return {"ok": True, "pets": [_public_pet(pet) for pet in _pet_catalog.list_pets()]}


@app.get("/pet/pets/{pet_id}/spritesheet")
def get_pet_spritesheet(pet_id: str):
    pet = _pet_catalog.get_pet(pet_id)
    if pet is None:
        raise HTTPException(status_code=404, detail=f"宠物不存在: {pet_id}")
    return FileResponse(pet["spritesheetPath"])


@app.post("/pet/pets")
async def install_pet(
    spritesheet: UploadFile = File(...),
    pet_id: str = Form(..., alias="petId"),
    display_name: str = Form(..., alias="displayName"),
    description: str = Form(""),
    sprite_version_number: int | None = Form(None, alias="spriteVersionNumber"),
):
    if spritesheet.size is not None and spritesheet.size > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="spritesheet 超过 50 MB 限制")
    try:
        pet = _pet_catalog.install(
            pet_id=pet_id,
            display_name=display_name,
            description=description,
            spritesheet=spritesheet.file,
            filename=spritesheet.filename or "spritesheet.webp",
            sprite_version_number=sprite_version_number,
        )
    except PetCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        await spritesheet.close()
    return {"ok": True, "pet": _public_pet(pet)}


@app.delete("/pet/pets/{pet_id}")
def delete_pet(pet_id: str):
    selected = _pet_catalog.load_settings().selected_pet_id == pet_id
    try:
        _pet_catalog.remove(pet_id)
    except PetCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if selected and _pet_process.running:
        _pet_process.stop()
        _pet_process.start()
    return {"ok": True}


@app.post("/pet/open")
def open_pet():
    settings = _pet_catalog.update_settings(enabled=True)
    _pet_process.start()
    return {"ok": True, "settings": settings.to_dict(), "running": _pet_process.running}


@app.post("/pet/close")
def close_pet():
    settings = _pet_catalog.update_settings(enabled=False)
    _pet_process.stop()
    return {"ok": True, "settings": settings.to_dict(), "running": False}


@app.get("/pet/state")
def get_pet_state():
    settings = _pet_catalog.load_settings()
    selected = _pet_catalog.get_pet(settings.selected_pet_id)
    return {
        "ok": True,
        "state": _pet_state.snapshot(),
        "approvals": [request.to_dict() for request in _approval_manager.get_pending()],
        "selectedPet": _public_pet(selected),
        "settings": settings.to_dict(),
    }


@app.post("/pet/runtime/position")
def save_pet_position(req: PetPositionRequest):
    settings = _pet_catalog.update_settings(
        position_x=req.x,
        position_y=req.y,
        update_position=True,
    )
    return {"ok": True, "position": settings.to_dict()["position"]}


@app.post("/pet/runtime/closed")
def pet_runtime_closed():
    settings = _pet_catalog.update_settings(enabled=False)
    return {"ok": True, "settings": settings.to_dict()}


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
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    if media_type.startswith("image/"):
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            content_disposition_type="inline",
        )
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type=media_type,
    )


# ==========================================================================
# Step 9 Routes — Skills
# ==========================================================================


@app.get("/skills")
def list_skills():
    """List all available skills."""
    skills = _skill_registry.list_skills(filter_unavailable=False)
    return {
        "ok": True,
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "hasAssets": bool(s.assets),
                "hasReferences": bool(s.references),
            }
            for s in skills
        ],
    }


@app.get("/skills/{name}")
def get_skill_detail(name: str):
    """Get full details for a single skill."""
    skill = _skill_registry.get_skill(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
    return {
        "ok": True,
        "skill": {
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "assets": [str(p) for p in skill.assets],
            "references": [str(p) for p in skill.references],
        },
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
    # Clean up workspace binding and unlimited mode
    _workspace_manager.unset(session_id)
    _workspace_manager.set_unlimited(session_id, False)
    # Clean up attachments directory
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
    target = prompts_dir() / filename
    tmp = target.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


# ==========================================================================
# Cron management
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

    from claw.scheduler.types import CronSchedule

    # Build schedule
    delete_after = False
    if req.every_seconds:
        schedule = CronSchedule(kind="every", every_ms=req.every_seconds * 1000)
    elif req.cron_expr:
        effective_tz = req.tz or default_timezone_name()
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
            effective_tz = req.tz or default_timezone_name()
            try:
                tz = ZoneInfo(effective_tz)
            except Exception:
                raise HTTPException(status_code=400, detail=f"未知时区: {effective_tz}")
            dt = dt.replace(tzinfo=tz)
        at_ms = int(dt.timestamp() * 1000)
        schedule = CronSchedule(kind="at", at_ms=at_ms)
        delete_after = True
    else:
        raise HTTPException(
            status_code=400,
            detail="需要 everySeconds、cronExpr 或 at 之一",
        )

    # 如果指定的 session 不存在，使用最近的 session 而非新建
    if not _session_store.exists(req.session_id):
        recent = _session_store.list_summaries()
        if recent:
            req.session_id = recent[0].session_id
        else:
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


# ==========================================================================
# WebUI runtime settings
# ==========================================================================


def _masked(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}****{value[-4:]}"


def _int_setting(name: str, default: int) -> int:
    raw = setting_value(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_setting(name: str, default: float) -> float:
    raw = setting_value(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _llm_settings_payload() -> dict[str, Any]:
    api_key = setting_value("LLM_API_KEY", "").strip()
    return {
        "baseUrl": setting_value("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
        "apiKeyMasked": _masked(api_key),
        "apiKeyConfigured": bool(api_key),
        "model": setting_value("LLM_MODEL", "").strip(),
        "contextWindow": _int_setting("LLM_CONTEXT_WINDOW", 32000),
        "contextUsageRatio": _float_setting("LLM_CONTEXT_USAGE_RATIO", 0.8),
        "maxOutputTokens": _int_setting("LLM_MAX_OUTPUT_TOKENS", 4096),
        "consolidationRatio": _float_setting("LLM_CONSOLIDATION_RATIO", 0.5),
    }


def _qq_settings_payload() -> dict[str, Any]:
    cfg = load_qq_config()
    return {
        "enabled": cfg.enabled,
        "appId": cfg.app_id,
        "clientSecretMasked": _masked(cfg.client_secret),
        "allowFrom": ",".join(cfg.allow_from),
        "msgFormat": "markdown" if cfg.markdown_support else "text",
        "ackMessage": cfg.ack_message,
    }


def _qq_connection_status() -> dict[str, Any]:
    cfg = load_qq_config()
    configured = bool(cfg.app_id and cfg.client_secret)
    ws = getattr(_qq_channel, "_ws", None)
    task_done = bool(_qq_task and _qq_task.done())
    running = bool(_qq_channel and _qq_channel.is_running and ws and not ws.closed)
    starting = bool(
        cfg.enabled
        and configured
        and _qq_channel
        and not task_done
        and not running
    )
    if running:
        message = "QQ 通道已连接"
    elif cfg.enabled and not configured:
        message = "QQ 通道已启用，但 QQ_APP_ID 或 QQ_CLIENT_SECRET 未配置"
    elif starting:
        message = "QQ 通道连接中"
    elif cfg.enabled and task_done:
        message = "QQ 通道连接失败，请检查 AppID / Client Secret 或 QQ 开放平台配置"
    elif cfg.enabled:
        message = "QQ 通道已启用，等待连接"
    else:
        message = "QQ 通道未启用"
    return {
        "enabled": cfg.enabled,
        "configured": configured,
        "running": running,
        "starting": starting,
        "appId": cfg.app_id,
        "message": message,
    }


def _validate_url(value: str, field_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"{field_name} 必须是完整的 http/https URL")


def _apply_llm_runtime_config() -> None:
    global _config
    settings = _llm_settings_payload()
    api_key = setting_value("LLM_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 LLM API Key")
    if not settings["model"]:
        raise HTTPException(status_code=400, detail="请填写 LLM 模型名称")
    _validate_url(settings["baseUrl"], "Base_url")

    _config = LLMConfig(
        api_key=api_key,
        base_url=settings["baseUrl"],
        model=settings["model"],
        context_window=settings["contextWindow"],
        context_usage_ratio=settings["contextUsageRatio"],
        max_output_tokens=settings["maxOutputTokens"],
        consolidation_ratio=settings["consolidationRatio"],
    )
    _llm_client.set_config(_config)


async def _apply_qq_runtime_config() -> None:
    global _qq_channel, _qq_task
    cfg = load_qq_config()

    if _qq_channel is not None:
        await _qq_channel.stop()
        _qq_channel = None
    if _qq_task is not None and not _qq_task.done():
        _qq_task.cancel()
        with suppress(asyncio.CancelledError):
            await _qq_task
    _qq_task = None

    if not cfg.enabled:
        return
    if not cfg.app_id or not cfg.client_secret:
        return

    _qq_channel = QQChannel(QQConfig(
        enabled=True,
        app_id=cfg.app_id,
        client_secret=cfg.client_secret,
        allow_from=list(cfg.allow_from),
        markdown_support=cfg.markdown_support,
        ack_message=cfg.ack_message,
    ))
    _qq_channel.set_message_handler(_qq_message_handler)
    _qq_channel.set_interaction_handler(_qq_interaction_handler)
    _qq_task = asyncio.create_task(_qq_channel.start())


async def _ensure_qq_runtime_started() -> None:
    cfg = load_qq_config()
    if not cfg.enabled or not cfg.app_id or not cfg.client_secret:
        return
    if _qq_channel is not None and _qq_channel.is_running and not (_qq_task and _qq_task.done()):
        return
    await _apply_qq_runtime_config()


class LLMSettingsRequest(BaseModel):
    base_url: str = Field(alias="baseUrl")
    api_key: str | None = Field(default=None, alias="apiKey")
    model: str
    context_window: int = Field(alias="contextWindow")
    context_usage_ratio: float = Field(alias="contextUsageRatio")
    max_output_tokens: int = Field(alias="maxOutputTokens")
    consolidation_ratio: float = Field(alias="consolidationRatio")


class QQSettingsRequest(BaseModel):
    enabled: bool
    app_id: str = Field(default="", alias="appId")
    client_secret: str | None = Field(default=None, alias="clientSecret")
    allow_from: str = Field(default="", alias="allowFrom")
    msg_format: str = Field(default="markdown", alias="msgFormat")
    ack_message: str = Field(default="", alias="ackMessage")


@app.get("/settings/llm")
def get_llm_settings():
    return {"ok": True, "settings": _llm_settings_payload()}


@app.put("/settings/llm")
def update_llm_settings(req: LLMSettingsRequest):
    _validate_url(req.base_url.strip(), "Base_url")
    if not req.model.strip():
        raise HTTPException(status_code=400, detail="请填写 LLM 模型名称")
    if req.context_window < 1024:
        raise HTTPException(status_code=400, detail="Context window 不能小于 1024")
    if req.context_usage_ratio <= 0 or req.context_usage_ratio > 1:
        raise HTTPException(status_code=400, detail="Context usage ratio 必须在 0 到 1 之间")
    if req.max_output_tokens < 1:
        raise HTTPException(status_code=400, detail="Max output tokens 必须大于 0")
    if req.consolidation_ratio <= 0 or req.consolidation_ratio > 1:
        raise HTTPException(status_code=400, detail="Consolidation ratio 必须在 0 到 1 之间")

    previous_settings = load_runtime_settings_raw()
    updates: dict[str, Any] = {
        "LLM_BASE_URL": req.base_url,
        "LLM_MODEL": req.model,
        "LLM_CONTEXT_WINDOW": req.context_window,
        "LLM_CONTEXT_USAGE_RATIO": req.context_usage_ratio,
        "LLM_MAX_OUTPUT_TOKENS": req.max_output_tokens,
        "LLM_CONSOLIDATION_RATIO": req.consolidation_ratio,
    }
    if req.api_key:
        updates["LLM_API_KEY"] = req.api_key
    update_runtime_settings(updates)
    try:
        _apply_llm_runtime_config()
    except HTTPException:
        replace_runtime_settings_raw(previous_settings)
        raise
    except Exception as exc:
        replace_runtime_settings_raw(previous_settings)
        raise HTTPException(status_code=400, detail=f"LLM 配置应用失败: {exc}") from exc
    return {"ok": True, "settings": _llm_settings_payload()}


@app.get("/settings/channel")
async def get_channel_settings():
    await _ensure_qq_runtime_started()
    return {
        "ok": True,
        "settings": {"qq": _qq_settings_payload()},
        "status": _qq_connection_status(),
    }


@app.put("/settings/channel/qq")
async def update_qq_settings(req: QQSettingsRequest):
    if req.enabled and not req.app_id.strip():
        raise HTTPException(status_code=400, detail="启用 QQ 通道前请填写 QQ_APP_ID")
    if req.msg_format not in {"markdown", "text"}:
        raise HTTPException(status_code=400, detail="QQ_MSG_FORMAT 只能是 markdown 或 text")

    updates: dict[str, Any] = {
        "QQ_ENABLED": "true" if req.enabled else "false",
        "QQ_APP_ID": req.app_id,
        "QQ_ALLOW_FROM": req.allow_from,
        "QQ_MSG_FORMAT": req.msg_format,
        "QQ_ACK_MESSAGE": req.ack_message,
    }
    if req.client_secret:
        updates["QQ_CLIENT_SECRET"] = req.client_secret
    update_runtime_settings(updates)
    await _apply_qq_runtime_config()
    return {
        "ok": True,
        "settings": {"qq": _qq_settings_payload()},
        "status": _qq_connection_status(),
    }


def _make_qr_data_uri(text: str) -> str:
    try:
        import qrcode
    except Exception as exc:
        raise HTTPException(status_code=500, detail="缺少 qrcode 依赖，无法生成二维码") from exc
    image = qrcode.make(text)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _prune_qq_onboard_tasks(max_age_seconds: int = 600) -> None:
    now = datetime.now(timezone.utc).timestamp()
    stale = [
        task_id for task_id, task in _qq_onboard_tasks.items()
        if now - float(task.get("created_at", 0)) > max_age_seconds
    ]
    for task_id in stale:
        _qq_onboard_tasks.pop(task_id, None)


@app.post("/settings/channel/qq/onboard/start")
def start_qq_onboard():
    _prune_qq_onboard_tasks()
    try:
        from claw.channels.qq_onboard import _create_bind_task, build_connect_url

        task_id, aes_key = _create_bind_task()
        connect_url = build_connect_url(task_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"创建 QQ 扫码任务失败: {exc}") from exc

    _qq_onboard_tasks[task_id] = {
        "aes_key": aes_key,
        "created_at": datetime.now(timezone.utc).timestamp(),
    }
    return {
        "ok": True,
        "taskId": task_id,
        "connectUrl": connect_url,
        "qrImage": _make_qr_data_uri(connect_url),
    }


@app.get("/settings/channel/qq/onboard/{task_id}")
async def poll_qq_onboard(task_id: str):
    _prune_qq_onboard_tasks()
    task = _qq_onboard_tasks.get(task_id)
    if not task:
        return {"ok": True, "status": "expired", "message": "二维码已过期，请重新发起扫码连接"}

    try:
        from claw.channels.qq_crypto import decrypt_secret
        from claw.channels.qq_onboard import BindStatus, _poll_bind_result

        status, app_id, encrypted_secret, user_openid = _poll_bind_result(task_id)
    except Exception as exc:
        return {"ok": True, "status": "pending", "message": f"等待扫码确认: {exc}"}

    if status == BindStatus.EXPIRED:
        _qq_onboard_tasks.pop(task_id, None)
        return {"ok": True, "status": "expired", "message": "二维码已过期，请重新发起扫码连接"}
    if status != BindStatus.COMPLETED:
        return {"ok": True, "status": "pending", "message": "等待手机 QQ 扫码确认"}

    client_secret = decrypt_secret(encrypted_secret, str(task["aes_key"]))
    allow_from = user_openid or setting_value("QQ_ALLOW_FROM", "*")
    update_runtime_settings({
        "QQ_ENABLED": "true",
        "QQ_APP_ID": app_id,
        "QQ_CLIENT_SECRET": client_secret,
        "QQ_ALLOW_FROM": allow_from,
        "QQ_MSG_FORMAT": setting_value("QQ_MSG_FORMAT", "markdown"),
    })
    _qq_onboard_tasks.pop(task_id, None)
    await _apply_qq_runtime_config()
    return {
        "ok": True,
        "status": "completed",
        "settings": {"qq": _qq_settings_payload()},
        "connection": _qq_connection_status(),
    }


# ==========================================================================
# QQ channel status endpoint
# ==========================================================================


@app.get("/qq/status")
def qq_status():
    """Get QQ channel status."""
    global _qq_channel
    if _qq_channel is None:
        return {
            "ok": True,
            "enabled": False,
            "running": False,
            "message": "QQ 通道未启用。在 .env 中设置 QQ_ENABLED=true 并配置 QQ_APP_ID / QQ_APP_SECRET。",
        }
    return {
        "ok": True,
        "enabled": True,
        "running": _qq_channel.is_running,
        "config": _qq_channel.config.to_dict(),
    }


# ---------------------------------------------------------------------------
# SSE streaming chat endpoint — real-time tool call visibility
# ---------------------------------------------------------------------------


def _event_to_sse(event: TurnEvent) -> str:
    """Serialize a TurnEvent to an SSE data line."""
    data = {"type": type(event).__name__}
    for field_name, value in event.__dict__.items():
        if value is not None and value != "" and value != [] and value != {}:
            data[field_name] = value
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/chat/stream")
async def handle_chat_stream(req: ChatRequest):
    """Send a user message and stream agent turn events via SSE.

    Events emitted:
      - ``ThinkingEvent`` — LLM is processing (iteration number)
      - ``ToolCallStartEvent`` — tool execution begins (name, args, call_id)
      - ``ToolCallEndEvent`` — tool execution completes (ok, result, error, duration_ms)
      - ``FinalEvent`` — agent turn complete with final reply
      - ``ErrorEvent`` — non-fatal error during turn
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

    # Same fail-safe as /chat: recognized commands are executed locally and
    # represented as a short SSE response without starting an agent turn.
    command = req.message.strip()
    if _is_slash_command(command):
        result_text = _execute_slash_command(command, sid)

        async def _command_events():
            yield _event_to_sse(FinalEvent(content=result_text))
            yield f"data: {json.dumps({'type': '_session_info', 'sessionId': sid, 'autoMode': _auto_mode.get(sid, False), 'unlimitedMode': _workspace_manager.is_unlimited(sid)}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"_done\"}\n\n"

        return StreamingResponse(
            _command_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if not _llm_ready():
        reply = _llm_missing_reply()
        try:
            session = _session_store.get(sid)
            session.append_message("user", req.message)
            session.append_message("assistant", reply)
            _session_store.save(session)
        except Exception:
            logger.exception("无法持久化 LLM 未配置提示: session=%s", sid)

        async def _config_required_events():
            yield _event_to_sse(FinalEvent(content=reply))
            yield f"data: {json.dumps({'type': '_session_info', 'sessionId': sid, 'autoMode': _auto_mode.get(sid, False), 'unlimitedMode': _workspace_manager.is_unlimited(sid)}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"_done\"}\n\n"

        return StreamingResponse(
            _config_required_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    cancel_event = _register_active_turn(sid)
    if cancel_event is None:
        raise HTTPException(status_code=409, detail="该 Session 已有任务正在运行，请等待完成或先停止任务。")

    # Per-session event queue (thread-safe)
    event_queue: queue.Queue = queue.Queue()
    done = threading.Event()

    def _event_callback(event: TurnEvent) -> None:
        _pet_state.handle_event(sid, event)
        event_queue.put(event)

    def _run_turn() -> None:
        """Run the agent turn in a background thread, emitting events."""
        _set_turn_session_id(sid)
        _update_cron_context(sid, req.session_id or "default")
        _pet_state.start_turn(sid, req.message)
        try:
            reply = run_agent_turn(
                sid,
                req.message,
                session_store=_session_store,
                context_builder=_context_builder,
                tool_registry=_tool_registry,
                llm_client=_llm_client,
                approval_handler=_gateway_approval_handler,
                compaction_worker=_compaction_worker,
                auto_mode=_auto_mode.get(sid, False),
                unlimited_mode=_workspace_manager.is_unlimited(sid),
                event_callback=_event_callback,
                cancel_event=cancel_event,
            )
            # The FinalEvent is emitted inside run_agent_turn, but we
            # also send a session_id + autoMode info event
            event_queue.put({"type": "_session_info", "sessionId": sid, "autoMode": _auto_mode.get(sid, False), "unlimitedMode": _workspace_manager.is_unlimited(sid)})

            # Auto-title
            try:
                session_obj = _session_store.get(sid)
                msgs = [{"role": m.role, "content": m.content} for m in session_obj.messages]
                new_title = auto_title_if_first_turn(sid, msgs, _session_store, _llm_client)
                if new_title:
                    event_queue.put({"type": "_title", "title": new_title})
            except Exception:
                pass
        except Exception as exc:
            _pet_state.finish_turn(sid, failed=True)
            logger.exception("流式 Agent turn 异常: session=%s", sid)
            fallback = _persist_agent_fallback(
                sid, "抱歉，Agent 在执行工具时意外中止。已保留现有进度，请重试。"
            )
            event_queue.put(ErrorEvent(error=f"Agent turn 异常: {exc}"))
            event_queue.put(FinalEvent(content=fallback))
        finally:
            _set_turn_session_id(None)
            _unregister_active_turn(sid, cancel_event)
            done.set()
            event_queue.put(None)  # sentinel

    # Run the agent turn in a thread
    loop = asyncio.get_running_loop()
    thread = threading.Thread(target=_run_turn, daemon=True)
    thread.start()

    async def _event_generator():
        """Yield SSE events from the queue until done."""
        while True:
            # Non-blocking check of the queue
            try:
                event = await loop.run_in_executor(None, lambda: event_queue.get(timeout=0.1))
            except queue.Empty:
                if done.is_set():
                    break
                # Send a keepalive comment to prevent proxy timeout
                yield ": keepalive\n\n"
                continue

            if event is None:  # sentinel
                break

            if isinstance(event, dict):
                # Internal info events
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                yield _event_to_sse(event)

        yield "data: {\"type\": \"_done\"}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ---------------------------------------------------------------------------
# Web UI — SPA static files (must be last so API routes take precedence)
# StaticFiles with html=True serves index.html as fallback for unknown paths.
# ---------------------------------------------------------------------------

if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="webui")
