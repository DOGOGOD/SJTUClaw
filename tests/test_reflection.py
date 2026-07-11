"""Tests for the daily memory reflection module.

Covers: ReflectionConfig serialisation, ``_parse_facts_from_response``,
``_same_day``, config load/save, ``update_config``, ``run_now`` with
mock LLM, session gathering, edge cases (empty, corrupted, invalid LLM
output).
"""

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claw.memory.reflection import (
    ReflectionConfig,
    ReflectionManager,
    ReflectionRun,
    _now_iso,
    _parse_facts_from_response,
    _same_day,
)
from claw.memory.store import MemoryStore
from claw.session.store import SessionStore
from claw.llm.client import LLMError


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(str(d), ignore_errors=True)


@pytest.fixture
def config_dir(tmp):
    d = tmp / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ms(tmp):
    return MemoryStore(tmp / "memory")


@pytest.fixture
def ss(tmp):
    s = SessionStore(tmp / "sessions")
    return s


@pytest.fixture
def mock_llm():
    """Return a MagicMock that returns a valid facts JSON by default."""
    llm = MagicMock()
    llm.chat.return_value = json.dumps([
        {"category": "project", "content": "测试项目使用FastAPI", "tags": ["fastapi", "python"], "importance": 4},
        {"category": "user_preference", "content": "用户喜欢简洁代码", "tags": ["style"], "importance": 3},
    ], ensure_ascii=False)
    return llm


@pytest.fixture
def mgr(config_dir, ms, ss, mock_llm):
    """Return a ReflectionManager with mock LLM."""
    return ReflectionManager(config_dir, ms, ss, mock_llm)


@pytest.fixture
def mgr_with_sessions(config_dir, ms, ss, mock_llm):
    """Return a ReflectionManager with pre-seeded sessions."""
    # Create sessions with messages
    s1 = ss.create_session(session_id="session_001", title="项目讨论")
    s1.append_message("user", "我正在用FastAPI开发智能客服系统")
    s1.append_message("assistant", "好的喵，FastAPI是个不错的选择")
    s1.append_message("user", "数据库我决定用PostgreSQL")
    s1.summary = "用户决定使用FastAPI+PostgreSQL开发智能客服系统"
    ss.save(s1)

    s2 = ss.create_session(session_id="session_002", title="杂谈")
    s2.append_message("user", "今天天气不错")
    s2.append_message("assistant", "是啊喵")
    ss.save(s2)

    return ReflectionManager(config_dir, ms, ss, mock_llm)


# =============================================================================
# ReflectionConfig
# =============================================================================


class TestReflectionConfig:
    """Serialisation and default values."""

    def test_defaults(self):
        cfg = ReflectionConfig()
        assert cfg.enabled is True
        assert cfg.time == "23:00"
        assert cfg.last_run_at == ""
        assert cfg.run_history == []

    def test_roundtrip(self):
        cfg = ReflectionConfig(
            enabled=False,
            time="09:00",
            last_run_at="2026-07-08T09:00:00",
            run_history=[
                {"runAt": "2026-07-07T09:00:00", "sessionsReviewed": 2, "factsExtracted": 3, "status": "success"}
            ],
        )
        data = cfg.to_dict()
        cfg2 = ReflectionConfig.from_dict(data)
        assert cfg2.enabled is False
        assert cfg2.time == "09:00"
        assert cfg2.last_run_at == "2026-07-08T09:00:00"
        assert len(cfg2.run_history) == 1

    def test_from_dict_partial(self):
        """Missing fields get defaults."""
        cfg = ReflectionConfig.from_dict({})
        assert cfg.enabled is True
        assert cfg.time == "23:00"
        assert cfg.run_history == []

    def test_from_dict_bad_history(self):
        """Non-list runHistory is coerced to empty list."""
        cfg = ReflectionConfig.from_dict({"runHistory": "not_a_list"})
        assert cfg.run_history == []

    def test_to_dict_null_last_run(self):
        """lastRunAt is None in JSON when empty string."""
        cfg = ReflectionConfig(last_run_at="")
        data = cfg.to_dict()
        assert data["lastRunAt"] is None


# =============================================================================
# _parse_facts_from_response
# =============================================================================


class TestParseFacts:
    """LLM response parsing — various real-world output shapes."""

    def test_clean_json_array(self):
        raw = '[{"category":"project","content":"测试","tags":["a"],"importance":4}]'
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 1
        assert facts[0]["category"] == "project"
        assert facts[0]["content"] == "测试"
        assert facts[0]["tags"] == ["a"]
        assert facts[0]["importance"] == 4

    def test_with_markdown_fence(self):
        raw = '```json\n[{"category":"fact","content":"用户在上海","tags":["location"]}]\n```'
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 1
        assert facts[0]["category"] == "fact"

    def test_with_surrounding_text(self):
        raw = '好的，以下是提取的记忆：\n[{"category":"decision","content":"决定用RESTful","tags":["api"]}]\n共1条。'
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 1
        assert facts[0]["category"] == "decision"

    def test_multiple_facts(self):
        raw = json.dumps([
            {"category": "project", "content": "项目A", "tags": ["a"]},
            {"category": "user_preference", "content": "偏好B", "tags": ["b"]},
            {"category": "fact", "content": "事实C", "tags": ["c"]},
        ])
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 3

    def test_empty_response(self):
        assert _parse_facts_from_response("") == []
        assert _parse_facts_from_response("   ") == []

    def test_no_array(self):
        """Response without a JSON array."""
        assert _parse_facts_from_response("这是一段纯文本没有JSON") == []

    def test_invalid_json(self):
        assert _parse_facts_from_response("[{bad json}]") == []

    def test_empty_array(self):
        raw = "[]"
        facts = _parse_facts_from_response(raw)
        assert facts == []

    def test_invalid_category_filtered(self):
        """Entries with invalid category are dropped."""
        raw = json.dumps([
            {"category": "invalid_cat", "content": "应该被过滤", "tags": []},
            {"category": "project", "content": "有效记忆", "tags": []},
        ])
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 1
        assert facts[0]["content"] == "有效记忆"

    def test_missing_content_filtered(self):
        """Entries without content are dropped."""
        raw = json.dumps([
            {"category": "project", "content": "", "tags": []},
            {"category": "fact", "content": "有效", "tags": []},
        ])
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 1

    def test_importance_clamped(self):
        """Out-of-range importance is reset to 3."""
        raw = json.dumps([
            {"category": "fact", "content": "test", "importance": 100},
        ])
        facts = _parse_facts_from_response(raw)
        assert facts[0]["importance"] == 3

    def test_tags_normalised(self):
        """Tags are lowercased and stripped (sorting happens in MemoryStore.add)."""
        raw = json.dumps([
            {"category": "fact", "content": "test", "tags": ["  PYTHON  ", "", "Java"]},
        ])
        facts = _parse_facts_from_response(raw)
        # _parse_facts normalises case and strips whitespace; order is preserved
        assert "python" in facts[0]["tags"]
        assert "java" in facts[0]["tags"]
        assert "" not in facts[0]["tags"]

    def test_non_dict_entries_skipped(self):
        raw = json.dumps(["not_a_dict", {"category": "fact", "content": "valid"}])
        facts = _parse_facts_from_response(raw)
        assert len(facts) == 1
        assert facts[0]["content"] == "valid"


# =============================================================================
# _same_day
# =============================================================================


class TestSameDay:
    def test_same_day_true(self):
        assert _same_day("2026-07-08T10:00:00", "2026-07-08T23:59:59") is True

    def test_same_day_false(self):
        assert _same_day("2026-07-08T10:00:00", "2026-07-09T10:00:00") is False

    def test_invalid_input(self):
        """Malformed timestamps return False without raising."""
        assert _same_day("not-a-date", "2026-07-08T10:00:00") is False
        assert _same_day("2026-07-08T10:00:00", "") is False
        assert _same_day("", "") is False


# =============================================================================
# Config persistence
# =============================================================================


class TestConfigPersistence:
    """Config load / save / update."""

    def test_save_and_reload(self, config_dir, ms, ss, mock_llm):
        mgr1 = ReflectionManager(config_dir, ms, ss, mock_llm)
        mgr1.update_config(enabled=False, time="08:30")
        assert mgr1.get_config()["enabled"] is False
        assert mgr1.get_config()["time"] == "08:30"

        # Reload from disk
        mgr2 = ReflectionManager(config_dir, ms, ss, mock_llm)
        assert mgr2.get_config()["enabled"] is False
        assert mgr2.get_config()["time"] == "08:30"

    def test_corrupted_config_fallback(self, config_dir, ms, ss, mock_llm):
        """Corrupted config file → fall back to defaults."""
        config_path = config_dir / "reflection_config.json"
        config_path.write_text("{not valid json", encoding="utf-8")
        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        assert mgr.get_config()["enabled"] is True
        assert mgr.get_config()["time"] == "23:00"

    def test_missing_config_file(self, config_dir, ms, ss, mock_llm):
        """No config file yet → defaults."""
        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        assert mgr.get_config()["enabled"] is True

    def test_update_config_rejects_bad_time(self, mgr):
        """Bad time format is silently ignored."""
        mgr.update_config(time="not-a-time")
        assert mgr.get_config()["time"] == "23:00"  # unchanged

    def test_update_config_accepts_valid_time(self, mgr):
        mgr.update_config(time="06:30")
        assert mgr.get_config()["time"] == "06:30"


# =============================================================================
# run_now — core reflection logic
# =============================================================================


class TestRunNow:
    """End-to-end reflection with mock LLM."""

    def test_run_now_extracts_and_saves(self, mgr_with_sessions, ms):
        mgr = mgr_with_sessions
        result = mgr.run_now()
        assert result["ok"] is True
        assert result["sessionsReviewed"] == 2
        assert result["factsExtracted"] >= 1

        # Facts should be saved to memory store
        entries = ms.list()
        assert len(entries) >= 1
        assert any("FastAPI" in e.content for e in entries)

    def test_run_now_records_history(self, mgr_with_sessions):
        mgr = mgr_with_sessions
        mgr.run_now()
        config = mgr.get_config()
        assert config["lastRunAt"] != ""  # updated
        assert len(config["runHistory"]) == 1
        assert config["runHistory"][0]["status"] == "success"

    def test_run_now_only_new_sessions(self, config_dir, ms, ss, mock_llm):
        """Second run with no new sessions should find nothing."""
        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)

        # First run — has sessions
        s = ss.create_session(session_id="s1", title="测试")
        s.append_message("user", "hello")
        ss.save(s)
        r1 = mgr.run_now()
        assert r1["sessionsReviewed"] == 1

        # Second run — no new sessions since last run
        r2 = mgr.run_now()
        assert r2["sessionsReviewed"] == 0
        assert r2["factsExtracted"] == 0

    def test_run_now_skips_empty_sessions(self, config_dir, ms, ss, mock_llm):
        """Sessions with 0 messages are skipped."""
        ss.create_session(session_id="empty_sess", title="空")
        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        result = mgr.run_now()
        assert result["sessionsReviewed"] == 0

    def test_run_now_handles_llm_error(self, config_dir, ms, ss):
        """Reflection should survive LLM failures gracefully."""
        llm = MagicMock()
        llm.chat.side_effect = LLMError("模拟的LLM故障")

        s = ss.create_session(session_id="s1", title="测试")
        s.append_message("user", "hi")
        ss.save(s)

        mgr = ReflectionManager(config_dir, ms, ss, llm)
        result = mgr.run_now()
        assert result["ok"] is False
        assert "LLM 调用失败" in result.get("error", "")

    def test_run_now_llm_returns_empty(self, mgr_with_sessions):
        """LLM returns empty array → no facts saved."""
        mgr_with_sessions._llm_client.chat.return_value = "[]"
        result = mgr_with_sessions.run_now()
        assert result["ok"] is True
        assert result["sessionsReviewed"] >= 1
        assert result["factsExtracted"] == 0

    def test_run_now_llm_returns_garbage(self, mgr_with_sessions, ms):
        """LLM returns unparseable text → no facts saved."""
        mgr_with_sessions._llm_client.chat.return_value = "I can't help with that."
        result = mgr_with_sessions.run_now()
        assert result["ok"] is True  # Not a failure — just nothing useful
        assert result["factsExtracted"] == 0

    def test_run_now_truncates_history(self, config_dir, ms, ss, mock_llm, monkeypatch):
        """run_history should be capped at MAX_RUN_HISTORY (50)."""
        from claw.memory import reflection as ref_mod

        # Make history limit small for quick testing
        monkeypatch.setattr(ref_mod, "_MAX_RUN_HISTORY", 3)

        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        for i in range(5):
            s = ss.create_session(session_id=f"s{i}", title=f"测试{i}")
            s.append_message("user", f"msg{i}")
            ss.save(s)
            mgr.run_now()

        config = mgr.get_config()
        assert len(config["runHistory"]) <= 3

    def test_run_now_skips_unchanged_sessions(self, config_dir, ms, ss, mock_llm):
        """Second run with no session changes should find 0 new sessions."""
        mock_llm.chat.return_value = json.dumps([
            {"category": "fact", "content": "唯一事实", "tags": ["unique"]}
        ])
        s = ss.create_session(session_id="s1", title="测试")
        s.append_message("user", "hello")
        ss.save(s)

        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        r1 = mgr.run_now()
        assert r1["sessionsReviewed"] == 1
        assert r1["factsExtracted"] == 1

        # Second run: session untouched since last run → skipped
        r2 = mgr.run_now()
        assert r2["sessionsReviewed"] == 0
        assert r2["factsExtracted"] == 0

    def test_run_now_reprocesses_modified_session(self, config_dir, ms, ss, mock_llm):
        """If a session gets new messages after reflection, it's re-processed."""
        import time
        mock_llm.chat.return_value = json.dumps([
            {"category": "fact", "content": "新事实", "tags": ["new"]}
        ])
        s = ss.create_session(session_id="s1", title="测试")
        s.append_message("user", "hello")
        ss.save(s)

        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        r1 = mgr.run_now()
        assert r1["factsExtracted"] == 1
        assert len(ms.list()) == 1

        # Ensure timestamp advances beyond second precision (ISO uses seconds)
        time.sleep(1.5)

        # Add new message to the session
        s2 = ss.get("s1")
        s2.append_message("user", "我还想补充一点，项目还需要支持WebSocket")
        ss.save(s2)

        # Second run should re-process the session
        r2 = mgr.run_now()
        assert r2["sessionsReviewed"] >= 1
        assert r2["factsExtracted"] == 1
        assert len(ms.list()) == 2  # new fact saved


# =============================================================================
# Session gathering
# =============================================================================


class TestSessionGathering:
    """``_gather_sessions`` builds correct snapshots."""

    def test_includes_summary_and_recent(self, config_dir, ms, ss, mock_llm):
        s = ss.create_session(session_id="s1", title="测试")
        s.append_message("user", "消息1")
        s.append_message("assistant", "回复1")
        s.summary = "之前的对话摘要"
        ss.save(s)

        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        sessions = mgr._gather_sessions()
        assert len(sessions) == 1
        ctx = sessions[0]["context"]
        assert "之前的对话摘要" in ctx
        assert "消息1" in ctx

    def test_truncates_long_messages(self, config_dir, ms, ss, mock_llm):
        s = ss.create_session(session_id="s1", title="测试")
        long_msg = "长" * 500
        s.append_message("user", long_msg)
        ss.save(s)

        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        sessions = mgr._gather_sessions()
        ctx = sessions[0]["context"]
        # Content should be truncated to 300 chars
        assert "..." in ctx or len(ctx) < len(long_msg) + 100

    def test_limits_recent_messages(self, config_dir, ms, ss, mock_llm):
        """Only last 20 messages are included."""
        s = ss.create_session(session_id="s1", title="测试")
        for i in range(30):
            s.append_message("user", f"消息{i}")
        ss.save(s)

        mgr = ReflectionManager(config_dir, ms, ss, mock_llm)
        sessions = mgr._gather_sessions()
        ctx = sessions[0]["context"]
        assert "消息0" not in ctx  # oldest, should be excluded
        assert "消息29" in ctx     # newest, should be included


# =============================================================================
# _save_facts
# =============================================================================


class TestSaveFacts:
    """Fact saving edge cases."""

    def test_saves_valid_facts(self, mgr, ms):
        facts = [
            {"category": "project", "content": "项目X", "tags": ["x"], "importance": 4},
            {"category": "fact", "content": "事实Y", "tags": [], "importance": 3},
        ]
        saved = mgr._save_facts(facts)
        assert saved == 2
        assert len(ms.list()) == 2

    def test_skips_invalid_facts(self, mgr, ms):
        """Invalid facts are silently skipped; good ones are still saved."""
        facts = [
            {"category": "invalid_cat", "content": "坏数据", "tags": []},
            {"category": "fact", "content": "好数据", "tags": [], "importance": 3},
        ]
        saved = mgr._save_facts(facts)
        assert saved == 1
        entries = ms.list()
        assert entries[0].content == "好数据"

    def test_skips_empty_content(self, mgr, ms):
        facts = [
            {"category": "fact", "content": "", "tags": []},
            {"category": "project", "content": "有效", "tags": []},
        ]
        saved = mgr._save_facts(facts)
        assert saved == 1


# =============================================================================
# Integration: memory round-trip via reflection
# =============================================================================


class TestReflectionMemoryRoundTrip:
    """Verify that facts extracted by reflection are usable via memory tools."""

    def test_extracted_facts_are_recallable(self, mgr_with_sessions, ms):
        from claw.tools.memory_tools import create_recall_tool, create_remember_tool
        from claw.tools.base import ToolRegistry

        # Run reflection to extract facts
        mgr_with_sessions.run_now()

        # Verify facts were saved
        entries = ms.list()
        assert len(entries) >= 1

        # Verify they are recallable
        results = ms.recall("FastAPI")
        assert len(results) >= 1
        assert any("FastAPI" in e.content for e in results)

    def test_reflection_does_not_affect_manual_memories(self, mgr_with_sessions, ms):
        """Manual memory entries coexist with reflection-extracted ones."""
        ms.add("手动添加的记忆", category="fact")
        mgr_with_sessions.run_now()
        entries = ms.list()
        manual_entries = [e for e in entries if e.source_session_id != "reflection"]
        assert len(manual_entries) >= 1
        assert manual_entries[0].content == "手动添加的记忆"


# =============================================================================
# ReflectionRun
# =============================================================================


class TestReflectionRun:
    def test_to_dict(self):
        run = ReflectionRun(
            run_at="2026-07-08T09:00:00",
            sessions_reviewed=3,
            facts_extracted=5,
            status="success",
        )
        d = run.to_dict()
        assert d["runAt"] == "2026-07-08T09:00:00"
        assert d["sessionsReviewed"] == 3
        assert d["factsExtracted"] == 5
        assert d["status"] == "success"

    def test_to_dict_with_error(self):
        run = ReflectionRun(
            run_at="2026-07-08T09:00:00",
            sessions_reviewed=1,
            facts_extracted=0,
            status="failure",
            error="LLM timeout",
        )
        d = run.to_dict()
        assert d["status"] == "failure"
        assert d["error"] == "LLM timeout"
