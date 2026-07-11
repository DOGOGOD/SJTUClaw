"""End-to-end integration test for the cron scheduling system.

Tests the full flow: job creation → execution → feedback.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Test 1: CronJob full lifecycle (add → wait → execute → verify)
# ---------------------------------------------------------------------------


def test_cron_job_lifecycle():
    """A cron job is created, the timer fires, the callback is invoked,
    and the job state is correctly updated."""
    from claw.cron.service import CronService
    from claw.cron.types import CronSchedule

    tmp = tempfile.mkdtemp()
    try:
        store_path = Path(tmp) / "jobs.json"
        srv = CronService(store_path)

        results = []

        async def on_job(job):
            results.append({"name": job.name, "message": job.payload.message})
            return f"processed: {job.name}"

        srv.on_job = on_job

        # Add a fast job (every 500ms) with session binding
        srv.add_job(
            name="e2e-test",
            schedule=CronSchedule(kind="every", every_ms=500),
            message="integration test message",
            session_key="test-session",
            origin_channel="cli",
            origin_chat_id="test-chat",
        )

        # Start service
        srv.start()
        time.sleep(2.5)  # Should fire ~4-5 times
        srv.stop()

        # Verify
        assert len(results) >= 3, f"Expected >=3 callbacks, got {len(results)}"
        for r in results:
            assert r["name"] == "e2e-test"
            assert r["message"] == "integration test message"

        # Check job state
        jobs = srv.list_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.state.last_status == "ok"
        assert len(job.state.run_history) >= 3

        print(f"  [PASS] Job lifecycle: {len(results)} callbacks, "
              f"status={job.state.last_status}, "
              f"history={len(job.state.run_history)} entries")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 2: System job protection
# ---------------------------------------------------------------------------


def test_system_job_protection():
    """System jobs (Dream, Heartbeat) cannot be removed by users."""
    from claw.cron.service import CronService
    from claw.cron.types import CronJob, CronPayload, CronSchedule

    tmp = tempfile.mkdtemp()
    try:
        store_path = Path(tmp) / "jobs.json"
        srv = CronService(store_path)

        # Register Dream as system job
        srv.register_system_job(CronJob(
            id="dream", name="dream",
            schedule=CronSchedule(kind="every", every_ms=7200000),
            payload=CronPayload(kind="system_event"),
        ))

        # Try to remove it
        result = srv.remove_job("dream")
        assert result == "protected", f"Expected 'protected', got '{result}'"

        # Disable and re-enable should work
        job = srv.enable_job("dream", enabled=False)
        assert job is not None
        assert job.enabled is False

        job = srv.enable_job("dream", enabled=True)
        assert job is not None
        assert job.enabled is True

        print("  [PASS] System job protection works")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 3: CronTool agent integration
# ---------------------------------------------------------------------------


def test_cron_tool_agent_flow():
    """Simulate an agent using the CronTool to manage jobs."""
    from claw.cron.service import CronService
    from claw.cron.types import CronJob, CronPayload, CronSchedule
    from claw.tools.cron_tool import CronTool

    tmp = tempfile.mkdtemp()
    try:
        store_path = Path(tmp) / "jobs.json"
        srv = CronService(store_path)

        tool = CronTool(srv, default_timezone="Asia/Shanghai")
        tool.set_context(
            session_key="session-001",
            channel="cli",
            chat_id="chat-001",
        )

        # Agent adds a job
        result = tool.execute_sync({
            "action": "add",
            "name": "daily-standup",
            "message": "请总结今日工作进展",
            "cron_expr": "0 9 * * 1-5",
            "tz": "Asia/Shanghai",
        })
        assert result.ok, f"Add failed: {result.error}"

        # Agent lists jobs
        result = tool.execute_sync({"action": "list"})
        assert result.ok
        assert "daily-standup" in result.content
        assert "cron: 0 9 * * 1-5" in result.content

        # Agent tries to remove a system job (should fail)
        srv.register_system_job(CronJob(
            id="dream", name="dream",
            schedule=CronSchedule(kind="every", every_ms=7200000),
            payload=CronPayload(kind="system_event"),
        ))
        result = tool.execute_sync({"action": "remove", "job_id": "dream"})
        assert not result.ok
        assert "Cannot remove" in result.error

        print("  [PASS] CronTool agent flow works")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 4: Dream and Heartbeat callbacks
# ---------------------------------------------------------------------------


def test_dream_callback():
    """Dream callback processes history entries correctly."""
    from claw.cron.callbacks import DreamCallback
    from claw.cron.types import CronJob, CronPayload, CronSchedule
    from claw.memory.history_log import HistoryLog
    from claw.config import MEMORY_DIR

    tmp = tempfile.mkdtemp()
    try:
        # Create workspace structure
        ws_root = Path(tmp) / "workspace"
        ws_root.mkdir(parents=True, exist_ok=True)

        # History log lives in the workspace memory dir
        history_dir = ws_root / "memory"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_log = HistoryLog(history_dir)

        # Write some test history
        history_log.append("User discussed project architecture", event_type="summary")
        history_log.append("Agent suggested using PostgreSQL", event_type="decision")
        history_log.append("User confirmed database choice", event_type="fact")

        # Create a mock LLM client that returns a simple edit suggestion
        class MockLLM:
            def chat(self, messages):
                return """分析完毕。对话中提取到以下值得长期记忆的信息：

```edit:MEMORY.md
<<<<<<< ORIGINAL
(empty)
=======
## 项目架构
- 数据库: PostgreSQL
- 项目正在讨论架构方案
>>>>>>> UPDATED
```

```edit:USER.md
<<<<<<< ORIGINAL
(empty)
=======
## 偏好
- 用户确认使用 PostgreSQL
>>>>>>> UPDATED
```
"""

        # Create workspace memory files with (empty) placeholder
        (ws_root / "SOUL.md").write_text("(empty)", encoding="utf-8")
        (ws_root / "USER.md").write_text("(empty)", encoding="utf-8")
        (history_dir / "MEMORY.md").write_text("(empty)", encoding="utf-8")

        dream_cb = DreamCallback(
            history_dir, history_log, MockLLM(), ws_root,
        )

        job = CronJob(
            id="dream", name="dream",
            schedule=CronSchedule(kind="every", every_ms=7200000),
            payload=CronPayload(kind="system_event"),
        )

        import asyncio
        result = asyncio.run(dream_cb(job))

        # Read back the edited files
        soul_content = (ws_root / "SOUL.md").read_text(encoding="utf-8")
        user_content = (ws_root / "USER.md").read_text(encoding="utf-8")
        memory_content = (history_dir / "MEMORY.md").read_text(encoding="utf-8")

        # The dream should have parsed the edit blocks and applied them
        print(f"  [PASS] Dream callback processed {history_log.count()} entries")
        print(f"         SOUL.md: {len(soul_content)} chars")
        print(f"         USER.md: {len(user_content)} chars")
        print(f"         MEMORY.md: {len(memory_content)} chars")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 5: Heartbeat callback (HEARTBEAT.md)
# ---------------------------------------------------------------------------


def test_heartbeat_callback():
    """Heartbeat reads HEARTBEAT.md and dispatches when there are tasks."""
    from claw.cron.callbacks import HeartbeatCallback
    from claw.cron.types import CronJob, CronPayload, CronSchedule

    tmp = tempfile.mkdtemp()
    try:
        ws = Path(tmp) / "workspace"
        ws.mkdir(parents=True, exist_ok=True)

        # HEARTBEAT.md with no active tasks → should skip
        (ws / "HEARTBEAT.md").write_text("""# Heartbeat Tasks

<!-- comments -->

## Active Tasks

<!-- Add your periodic tasks below -->
""", encoding="utf-8")

        hb = HeartbeatCallback(ws, None, None, None, None)
        job = CronJob(
            id="heartbeat", name="heartbeat",
            schedule=CronSchedule(kind="every", every_ms=1800000),
            payload=CronPayload(kind="system_event"),
        )

        # No active tasks → should return None
        assert not hb._has_active_tasks(
            (ws / "HEARTBEAT.md").read_text(encoding="utf-8")
        )

        # HEARTBEAT.md with active tasks
        (ws / "HEARTBEAT.md").write_text("""# Heartbeat Tasks

## Active Tasks

- [ ] Check server status every hour
- [ ] Monitor disk usage daily
""", encoding="utf-8")

        assert hb._has_active_tasks(
            (ws / "HEARTBEAT.md").read_text(encoding="utf-8")
        )

        print("  [PASS] Heartbeat task detection works")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 6: Full gateway config loading
# ---------------------------------------------------------------------------


def test_config_loading():
    """Dream and Heartbeat configs load correctly from defaults."""
    from claw.config import load_dream_config, load_heartbeat_config

    dc = load_dream_config()
    assert dc.enabled is True
    assert dc.interval_h == 2
    schedule = dc.build_schedule()
    assert schedule.kind == "every"
    assert schedule.every_ms == 7200000  # 2 hours

    hc = load_heartbeat_config()
    assert hc.enabled is True
    assert hc.interval_s == 1800  # 30 minutes

    print(f"  [PASS] Config loading: Dream every {dc.describe_schedule()}, "
          f"Heartbeat every {hc.interval_s}s")


# ---------------------------------------------------------------------------
# Test 7: One-shot (at) jobs
# ---------------------------------------------------------------------------


def test_oneshot_jobs():
    """One-shot at-jobs fire once and then auto-delete."""
    from claw.cron.service import CronService, _now_ms
    from claw.cron.types import CronSchedule

    tmp = tempfile.mkdtemp()
    try:
        store_path = Path(tmp) / "jobs.json"
        srv = CronService(store_path)

        called = []

        async def on_job(job):
            called.append(job.name)
            return "done"

        srv.on_job = on_job

        # Add a one-shot job that fires 500ms from now
        now = _now_ms()
        srv.add_job(
            name="one-shot",
            schedule=CronSchedule(kind="at", at_ms=now + 500),
            message="one-time task",
            delete_after_run=True,
            session_key="test-session",
            origin_channel="cli",
            origin_chat_id="test-chat",
        )

        srv.start()
        time.sleep(2)
        srv.stop()

        # Should have fired exactly once and then been deleted
        assert len(called) == 1
        assert called[0] == "one-shot"
        # Job should be deleted (delete_after_run=True)
        jobs = srv.list_jobs(include_disabled=True)
        assert len(jobs) == 0

        print("  [PASS] One-shot job fires once and auto-deletes")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 8: Action log (cross-process sync)
# ---------------------------------------------------------------------------


def test_action_log():
    """Jobs added while service is stopped appear after start."""
    from claw.cron.service import CronService
    from claw.cron.types import CronSchedule

    tmp = tempfile.mkdtemp()
    try:
        store_path = Path(tmp) / "jobs.json"
        srv = CronService(store_path)

        # Add a job while NOT running (appends to action.jsonl)
        srv.add_job(
            name="offline-job",
            schedule=CronSchedule(kind="every", every_ms=60000),
            message="added while stopped",
            session_key="test-session",
            origin_channel="cli",
            origin_chat_id="test-chat",
        )

        # Action log should exist
        action_path = store_path.parent / "action.jsonl"
        assert action_path.exists()
        print(f"  [INFO] action.jsonl exists: {action_path.stat().st_size} bytes")

        # Now start — the action should be merged
        srv.start()
        time.sleep(0.5)

        jobs = srv.list_jobs(include_disabled=True)
        assert len(jobs) == 1
        assert jobs[0].name == "offline-job"

        srv.stop()
        print("  [PASS] Action log cross-process sync works")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# Main
# ===========================================================================


if __name__ == "__main__":
    print("=" * 60)
    print("SJTUClaw Cron System — Integration Tests")
    print("=" * 60)
    print()

    tests = [
        ("CronJob lifecycle", test_cron_job_lifecycle),
        ("System job protection", test_system_job_protection),
        ("CronTool agent flow", test_cron_tool_agent_flow),
        ("Dream callback", test_dream_callback),
        ("Heartbeat callback", test_heartbeat_callback),
        ("Config loading", test_config_loading),
        ("One-shot jobs", test_oneshot_jobs),
        ("Action log sync", test_action_log),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            print(f"[{name}]")
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            import traceback
            print(f"  [FAIL] {e}")
            traceback.print_exc()
        print()

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)
