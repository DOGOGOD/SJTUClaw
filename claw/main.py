"""claw entry point — v7 with nanobot-compatible cron scheduling.

Usage:
    python -m claw.main
"""

from __future__ import annotations

import sys
from pathlib import Path

from claw.approval.manager import ApprovalManager
from claw.cli.repl import run_repl
from claw.config import (
    DATA_DIR,
    MEMORY_DIR,
    SESSIONS_DIR,
    ConfigError,
    load_compaction_config,
    load_config,
    load_dream_config,
    load_heartbeat_config,
)
from claw.context.builder import ContextBuilder
from claw.context.compaction_worker import CompactionWorker
from claw.cron.callbacks import (
    DreamCallback,
    HeartbeatCallback,
    make_dream_system_job,
    make_heartbeat_system_job,
)
from claw.cron.service import CronService
from claw.llm.client import LLMClient
from claw.memory.history_log import HistoryLog
from claw.memory.store import MemoryStore
from claw.prompts import PromptLoadError, load_soul, load_system_prompt
from claw.session.store import SessionStore
from claw.skills.registry import SkillRegistry
from claw.tools.base import ToolRegistry
from claw.workspace.manager import WorkspaceManager
from claw.memory.reflection import ReflectionManager


def _force_utf8_streams() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_streams()

    try:
        config = load_config()
        system_prompt = load_system_prompt()
        soul = load_soul()
    except (ConfigError, PromptLoadError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 1

    client = LLMClient(config)
    session_store = SessionStore(SESSIONS_DIR)
    memory_store = MemoryStore(MEMORY_DIR)

    # -- Cross-session history log
    compact_cfg = load_compaction_config()
    history_log = HistoryLog(
        MEMORY_DIR,
        max_entries=compact_cfg.max_history_entries,
    )

    # -- Context builder ----------------------------------------------------
    context_builder = ContextBuilder(
        system_prompt,
        soul,
        memory_store,
        history_log=history_log,
        workspace_path=str(SESSIONS_DIR.parent),
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
    compaction_worker.start_idle_compaction()

    workspace_manager = WorkspaceManager()
    approval_manager = ApprovalManager()

    # -- Skill registry -----------------------------------------------------
    skill_registry = SkillRegistry()
    context_builder.set_skill_registry(skill_registry)

    tool_registry = ToolRegistry()
    from claw.tools.readonly import register_all_readonly

    register_all_readonly(tool_registry)

    # -- Cron service (nanobot-compatible) ----------------------------------
    cron_store_path = DATA_DIR / "cron" / "jobs.json"
    cron_service = CronService(cron_store_path)

    # -- Dream system job ---------------------------------------------------
    dream_cfg = load_dream_config()
    if dream_cfg.enabled:
        dream_cb = DreamCallback(
            MEMORY_DIR, history_log, client,
            workspace_root=SESSIONS_DIR.parent,
        )
        cron_service.on_job = _make_dispatcher(
            cron_service, dream_cb=dream_cb
        )
        cron_service.register_system_job(make_dream_system_job(dream_cfg))
        print(f"[启动] Dream: {dream_cfg.describe_schedule()}")
    else:
        print("[启动] Dream: 已禁用")

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
        cron_service.on_job = _make_dispatcher(
            cron_service, dream_cb=dream_cb if dream_cfg.enabled else None,
            heartbeat_cb=hb_cb,
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
            history_log=history_log,
            cron_service=cron_service,
        )
    finally:
        cron_service.stop()
        compaction_worker.stop_idle_compaction()
        reflection_mgr.stop()
    return 0


def _make_dispatcher(
    cron_service,
    dream_cb=None,
    heartbeat_cb=None,
):
    """Build a dispatcher that routes cron jobs to their callbacks."""

    async def dispatch(job) -> str | None:
        if job.name == "dream" and dream_cb is not None:
            return await dream_cb(job)
        if job.name == "heartbeat" and heartbeat_cb is not None:
            return await heartbeat_cb(job)

        # For user-created agent_turn jobs, run through agent loop
        if job.payload.kind == "agent_turn" and job.payload.message:
            # In CLI mode, just print a reminder — the full session-bound
            # delivery path requires channel infrastructure.
            print(
                f"[cron] 作业 '{job.name}' 触发: {job.payload.message[:200]}"
            )
            return None

        return None

    return dispatch


if __name__ == "__main__":
    sys.exit(main())
