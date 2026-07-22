"""claw entry point — v7 with cron scheduling.

Usage:
    python -m claw.main
"""

from __future__ import annotations

import sys
import os

from claw.approval.manager import ApprovalManager
from claw.cli.repl import run_repl
from claw.config import (
    DATA_DIR,
    MEMORY_DIR,
    SESSIONS_DIR,
    ConfigError,
    LLMConfig,
    load_compaction_config,
    load_config,
    load_heartbeat_config,
)
from claw.context.builder import ContextBuilder
from claw.context.compaction_worker import CompactionWorker
from claw.scheduler.callbacks import (
    HeartbeatCallback,
    make_heartbeat_system_job,
)
from claw.scheduler import CronService
from claw.llm.client import LLMClient
from claw.pi import create_agent_client, is_pi_backend
from claw.memory.reflection import ReflectionManager
from claw.memory.store import MemoryStore
from claw.prompts import PromptLoadError, load_soul, load_system_prompt
from claw.session.store import SessionStore
from claw.skills.registry import SkillRegistry
from claw.tools.base import ToolRegistry
from claw.utils import default_timezone_name, force_utf8_stdio
from claw.workspace.manager import WorkspaceManager
from claw.pet.catalog import PetCatalog
from claw.pet.process import PetProcessManager


def main() -> int:
    force_utf8_stdio()

    # Configure logging so tool call output is visible in CLI mode
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    try:
        system_prompt = load_system_prompt()
        soul = load_soul()
    except PromptLoadError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 1

    try:
        config = load_config()
    except ConfigError as exc:
        if not is_pi_backend():
            print(f"[配置错误] {exc}", file=sys.stderr)
            return 1
        config = LLMConfig(
            api_key="",
            base_url="https://api.openai.com/v1",
            model="",
        )
        logging.info("Pi Agent 已启用；辅助 LLM 未配置。")

    client = create_agent_client(config)
    session_store = SessionStore(SESSIONS_DIR)
    memory_store = MemoryStore(MEMORY_DIR)
    workspace_manager = WorkspaceManager()

    # -- Context builder ----------------------------------------------------
    compact_cfg = load_compaction_config()
    context_builder = ContextBuilder(
        system_prompt,
        soul,
        memory_store,
        workspace_path=str(SESSIONS_DIR.parent),
        timezone=default_timezone_name(),
        workspace_manager=workspace_manager,
    )

    # -- Compaction worker (v3: with idle auto-compaction) ------------------
    compact_llm: LLMClient | None = None
    if compact_cfg.model:
        from claw.config import LLMConfig as LC

        compact_llm_config = LC(
            api_key=compact_cfg.api_key or config.api_key,
            base_url=compact_cfg.base_url or config.base_url,
            model=compact_cfg.model,
            context_window=config.context_window,
            context_usage_ratio=config.context_usage_ratio,
        )
        compact_llm = LLMClient(compact_llm_config)
    compaction_worker = CompactionWorker(
        client,
        session_store,
        compact_llm=compact_llm,
        config=compact_cfg,
        idle_ttl_minutes=compact_cfg.idle_ttl_minutes,
    )
    if not is_pi_backend():
        compaction_worker.start_idle_compaction()

    approval_manager = ApprovalManager()

    tool_registry = ToolRegistry()
    from claw.tools.readonly import register_all_readonly

    register_all_readonly(tool_registry)

    # -- Skill registry -----------------------------------------------------
    skill_registry = SkillRegistry()
    context_builder.set_skill_registry(skill_registry)

    # -- Cron service --------------------------------------------------------
    cron_store_path = DATA_DIR / "cron" / "jobs.json"
    cron_service = CronService(cron_store_path)

    # -- Desktop pet ---------------------------------------------------------
    pet_catalog = PetCatalog(DATA_DIR)
    gateway_host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    if gateway_host in {"0.0.0.0", "::"}:
        gateway_host = "127.0.0.1"
    pet_process = PetProcessManager(
        f"http://{gateway_host}:{os.getenv('GATEWAY_PORT', '8000')}",
        DATA_DIR,
    )

    # -- Heartbeat system job -----------------------------------------------
    hb_cfg = load_heartbeat_config()
    if hb_cfg.enabled:
        hb_cb = HeartbeatCallback(
            SESSIONS_DIR.parent,
            session_store,
            context_builder,
            tool_registry,
            client,
            keep_recent_messages=hb_cfg.keep_recent_messages,
        )
        from claw.scheduler.dispatcher import create_cron_dispatcher

        async def _cli_deliver(channel, chat_id, reply, metadata):
            if channel in ("cli", ""):
                print(f"\n[cron] 定时任务 '{chat_id}' 触发:")
                print(f"  {reply}\n")
            else:
                print(f"[cron] 未知 channel '{channel}'，跳过投递")

        cron_service.on_job = create_cron_dispatcher(
            session_store=session_store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
            set_turn_session_id=_set_turn_session_id,
            update_cron_context=lambda sid, chat_id, **kw: _update_cron_context(
                sid, chat_id, tool_registry, **kw
            ),
            on_heartbeat=hb_cb,
            on_deliver=_cli_deliver,
        )
        cron_service.register_system_job(make_heartbeat_system_job(hb_cfg))
        print(f"[启动] Heartbeat: 每 {hb_cfg.interval_s}s")

    # Start cron service
    cron_service.start()

    # -- Daily reflection manager -------------------------------------------
    reflection_mgr = ReflectionManager(
        MEMORY_DIR, memory_store, session_store, client
    )
    reflection_mgr.start()

    try:
        run_repl(
            client,
            session_store,
            memory_store,
            context_builder,
            tool_registry,
            workspace_manager=workspace_manager,
            approval_manager=approval_manager,
            skill_registry=skill_registry,
            reflection_manager=reflection_mgr,
            compaction_worker=compaction_worker,
            llm_config=config,
            cron_service=cron_service,
            pet_catalog=pet_catalog,
            pet_process=pet_process,
        )
    finally:
        cron_service.stop()
        compaction_worker.stop_idle_compaction()
        reflection_mgr.stop()
    return 0

# Module-level threading local for tool session id
import threading as _threading
_turn_local = _threading.local()


def _set_turn_session_id(sid: str | None) -> None:
    """Set the per-thread session id for tool handlers."""
    _turn_local.session_id = sid


def _get_turn_session_id() -> str:
    """Return the per-thread session id."""
    return getattr(_turn_local, "session_id", None) or "default"


def _update_cron_context(
    session_key: str,
    chat_id: str,
    registry=None,
    channel: str = "cli",
    metadata: dict | None = None,
) -> None:
    """Update the cron tool's session context."""
    if registry is None:
        return
    cron_tool = registry.get_tool("cron")
    if cron_tool is not None and hasattr(cron_tool, "set_context"):
        cron_tool.set_context(
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            metadata=metadata,
        )


if __name__ == "__main__":
    sys.exit(main())
