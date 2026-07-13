from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest


def _skill_doc(name: str, description: str = "test skill") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        "# Workflow\n\nDo the requested work safely.\n"
    )


def test_skill_registry_loads_categories_and_isolates_bad_skill(tmp_path: Path):
    from claw.skills.registry import SkillRegistry

    good = tmp_path / "writing" / "good-skill"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(_skill_doc("good-skill"), encoding="utf-8")

    bad = tmp_path / "bad-skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text("not frontmatter", encoding="utf-8")

    registry = SkillRegistry(skills_dir=tmp_path)

    assert registry.get_skill("good-skill") is not None
    assert any("bad-skill" in error for error in registry.load_errors)


def test_skill_manager_rejects_category_path_traversal(tmp_path: Path, monkeypatch):
    import claw.tools.skill_manager_tool as manager

    monkeypatch.setattr(manager, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(manager, "ARCHIVE_DIR", tmp_path / ".archive")

    result = manager._create_skill("safe-skill", _skill_doc("safe-skill"), "../escape")

    assert result["success"] is False
    assert not (tmp_path.parent / "escape").exists()


def test_context_builder_exposes_index_and_loads_selected_skill(tmp_path: Path):
    from claw.context.builder import ContextBuilder
    from claw.memory.store import MemoryStore
    from claw.session.store import SessionStore
    from claw.skills.registry import SkillRegistry

    skill_dir = tmp_path / "skills" / "focused-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _skill_doc("focused-skill", "handle focused tasks"), encoding="utf-8"
    )
    registry = SkillRegistry(skills_dir=tmp_path / "skills")
    builder = ContextBuilder("system", "soul", MemoryStore(tmp_path / "memory"))
    builder.set_skill_registry(registry)
    session = SessionStore(tmp_path / "sessions").create_session()

    messages = builder.build_messages(session)
    joined = "\n".join(str(message["content"]) for message in messages)
    injected = builder.build_skill_injection_message("focused-skill", "do it")

    assert "focused-skill" in joined
    assert "Do the requested work safely" not in joined
    assert "Do the requested work safely" in injected
    assert injected.endswith("do it")


def test_cron_service_rejects_ambiguous_or_non_runnable_schedules(tmp_path: Path):
    from claw.scheduler.service import CronService
    from claw.scheduler.types import CronSchedule

    service = CronService(tmp_path / "jobs.json")

    with pytest.raises(ValueError, match="every_ms"):
        service.add_job("bad-every", CronSchedule(kind="every", every_ms=0), "x")
    with pytest.raises(ValueError, match="future"):
        service.add_job(
            "past", CronSchedule(kind="at", at_ms=int(time.time() * 1000) - 1), "x"
        )
    with pytest.raises(ValueError, match="invalid cron expression"):
        service.add_job("bad-cron", CronSchedule(kind="cron", expr="not cron"), "x")


def test_cron_tool_requires_exactly_one_timing_parameter(tmp_path: Path):
    from claw.scheduler.service import CronService
    from claw.tools.cron_tool import CronTool

    tool = CronTool(CronService(tmp_path / "jobs.json"))
    tool.set_context("session", "cli", "chat")
    result = tool.execute_sync(
        {
            "action": "add",
            "name": "ambiguous",
            "message": "hello",
            "delay_seconds": 5,
            "every_seconds": 10,
        }
    )

    assert not result.ok
    assert "exactly one timing parameter" in (result.error or "")


def test_cron_tool_returns_error_for_invalid_numeric_input(tmp_path: Path):
    from claw.scheduler.service import CronService
    from claw.tools.cron_tool import CronTool

    tool = CronTool(CronService(tmp_path / "jobs.json"))
    tool.set_context("session", "cli", "chat")
    result = tool.execute_sync(
        {
            "action": "add",
            "name": "invalid-number",
            "message": "hello",
            "delay_seconds": "later",
        }
    )

    assert not result.ok
    assert "positive integer" in (result.error or "")


def test_cron_dispatcher_binds_context_inside_worker_thread(tmp_path: Path, monkeypatch):
    import claw.scheduler.dispatcher as dispatcher_module
    from claw.scheduler.dispatcher import create_cron_dispatcher
    from claw.scheduler.types import CronJob, CronPayload, CronSchedule

    local = threading.local()
    observed: dict[str, object] = {}

    class SessionStore:
        def exists(self, _sid):
            return True

    class CronGuard:
        def set_cron_context(self, active):
            previous = getattr(local, "in_cron", False)
            local.in_cron = active
            return previous

        def reset_cron_context(self, token):
            local.in_cron = token

    class Registry:
        def __init__(self):
            self.cron = CronGuard()

        def get_tool(self, name):
            return self.cron if name == "cron" else None

    registry = Registry()

    def set_session(sid):
        local.sid = sid

    def update_context(sid, chat_id, **kwargs):
        observed["context_thread"] = threading.get_ident()
        observed["metadata"] = kwargs.get("metadata")

    def fake_run(session_id, _message, **_kwargs):
        observed["worker_thread"] = threading.get_ident()
        observed["session_id"] = getattr(local, "sid", None)
        observed["in_cron"] = getattr(local, "in_cron", False)
        observed["input_event"] = _kwargs.get("input_event")
        return "done"

    monkeypatch.setattr(dispatcher_module, "run_agent_turn", fake_run)
    dispatch = create_cron_dispatcher(
        session_store=SessionStore(),
        context_builder=object(),
        tool_registry=registry,
        llm_client=object(),
        set_turn_session_id=set_session,
        update_cron_context=update_context,
    )
    job = CronJob(
        id="job",
        name="job",
        schedule=CronSchedule(kind="every", every_ms=1000),
        payload=CronPayload(
            message="run",
            session_key="bound-session",
            origin_channel="cli",
            origin_chat_id="chat",
            origin_metadata={"source": "test"},
        ),
    )

    assert asyncio.run(dispatch(job)) == "done"
    assert observed["context_thread"] == observed["worker_thread"]
    assert observed["session_id"] == "bound-session"
    assert observed["in_cron"] is True
    assert observed["input_event"] == "cron_trigger"
    assert observed["metadata"] == {"source": "test"}


def test_cron_messages_hide_internal_prompt_and_legacy_duplicate():
    from claw.scheduler.session_turns import visible_session_messages
    from claw.session.models import Session

    session = Session(session_id="session", title="test")
    session.append_message("user", "normal question")
    session.append_message(
        "user", "[定时任务: reminder]\n\ninternal prompt", injected_event="cron_trigger"
    )
    session.append_message("assistant", "time to rest")
    session.append_message("assistant", "[定时任务回复]\n\ntime to rest")

    visible = visible_session_messages(session)

    assert visible == [
        {"role": "user", "content": "normal question"},
        {"role": "assistant", "content": "time to rest"},
    ]


def test_cron_timer_rearms_after_tick_failure(tmp_path: Path):
    from claw.scheduler.service import CronService
    from claw.scheduler.types import CronStore

    service = CronService(tmp_path / "jobs.json")
    service._running = True
    service._store = CronStore()
    service._load_store = lambda: None
    service._record_heartbeat = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("heartbeat failed")
    )
    calls: list[bool] = []
    service._arm_timer = lambda: calls.append(True)

    asyncio.run(service._on_timer())

    assert calls == [True]
    assert service._timer_active is False
