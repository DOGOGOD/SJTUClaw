"""Pytest suite for SJTUClaw core modules.

Covers: SessionStore CRUD+persistence, ContextBuilder assembly order,
Compaction trigger/failure-protection, ToolRegistry param validation,
Workspace boundary enforcement, Approval approve/reject flow.
"""
import json
import shutil
import tempfile
from pathlib import Path

import pytest


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(str(d), ignore_errors=True)


@pytest.fixture
def ss(tmp):
    from claw.session.store import SessionStore
    return SessionStore(tmp / "sessions")


@pytest.fixture
def ms(tmp):
    from claw.memory.store import MemoryStore
    return MemoryStore(tmp / "memory.json")


@pytest.fixture
def reg():
    from claw.tools.base import ToolRegistry
    from claw.tools.readonly import register_all_readonly
    r = ToolRegistry()
    register_all_readonly(r)
    return r


@pytest.fixture
def wm():
    from claw.workspace.manager import WorkspaceManager
    return WorkspaceManager()


@pytest.fixture
def am():
    from claw.approval.manager import ApprovalManager
    return ApprovalManager()


@pytest.fixture
def cb(ms):
    from claw.context.builder import ContextBuilder
    return ContextBuilder("sp", "soul", ms)


# ═════════════════════════════════════════════════════════════════════
# SessionStore
# ═════════════════════════════════════════════════════════════════════

class TestSessionStore:
    def test_create_get(self, ss):
        s = ss.create_session(title="t")
        assert ss.get(s.session_id).title == "t"

    def test_duplicate_id_raises(self, ss):
        ss.create_session(session_id="dup")
        with pytest.raises(Exception):
            ss.create_session(session_id="dup")

    def test_list(self, ss):
        ss.create_session(); ss.create_session()
        assert len(ss.list_summaries()) == 2

    def test_rename(self, ss):
        s = ss.create_session(title="old")
        ss.rename(s.session_id, "new")
        assert ss.get(s.session_id).title == "new"

    def test_delete(self, ss):
        s = ss.create_session()
        ss.delete(s.session_id)
        assert not ss.exists(s.session_id)

    def test_get_nonexistent(self, ss):
        from claw.session.store import SessionNotFoundError
        with pytest.raises(SessionNotFoundError):
            ss.get("nope")

    def test_persist_reload(self, ss, tmp):
        from claw.session.store import SessionStore
        s = ss.create_session(session_id="pst", title="p")
        s.append_message("user", "hi")
        s.append_message("assistant", "hey")
        ss.save(s)
        s2 = SessionStore(tmp / "sessions").get("pst")
        assert len(s2.messages) == 2
        assert s2.messages[0].content == "hi"

    def test_corrupted_warns(self, ss, tmp):
        from claw.session.store import SessionStore
        s = ss.create_session(session_id="corr")
        ss.save(s)
        (tmp / "sessions" / "corr" / "session.json").write_text("{bad", encoding="utf-8")
        s2 = SessionStore(tmp / "sessions")
        assert len(s2.load_warnings) >= 1


# ═════════════════════════════════════════════════════════════════════
# ContextBuilder
# ═════════════════════════════════════════════════════════════════════

class TestContextBuilder:
    def test_order(self, cb, ss):
        cb._memory_store.add("pref: zh")
        s = ss.create_session()
        s.append_message("user", "q"); s.append_message("assistant", "a")
        msgs = cb.build_messages(s)
        roles = [m["role"] for m in msgs]
        assert roles[0] == "system"; assert roles[1] == "system"
        assert roles[-2] == "user"; assert roles[-1] == "assistant"

    def test_empty_memory_excluded(self, cb, ss):
        s = ss.create_session()
        all_text = " ".join(m["content"] for m in cb.build_messages(s))
        assert "长期记忆" not in all_text

    def test_skill_block_absent_without_registry(self, cb, ss):
        s = ss.create_session()
        all_text = " ".join(m["content"] for m in cb.build_messages(s))
        assert "可用 Skills" not in all_text


# ═════════════════════════════════════════════════════════════════════
# Compaction
# ═════════════════════════════════════════════════════════════════════

class TestCompaction:
    def test_not_needed_short(self, ss):
        from claw.context.compaction import needs_compaction
        s = ss.create_session()
        s.append_message("user", "hi"); s.append_message("assistant", "ok")
        assert not needs_compaction(s)

    def test_needed_by_count(self, ss):
        from claw.context.compaction import needs_compaction
        s = ss.create_session()
        for i in range(14):
            s.append_message("user" if i % 2 == 0 else "assistant", f"m{i}")
        assert needs_compaction(s)

    def test_needed_by_chars(self, ss):
        from claw.context.compaction import needs_compaction
        s = ss.create_session()
        long_msg = "x" * 600
        for _ in range(7):
            s.append_message("user", long_msg)
        assert needs_compaction(s)

    def test_fails_with_few_msgs(self, ss):
        from claw.context.compaction import CompactionError, compact_session
        s = ss.create_session()
        with pytest.raises(CompactionError):
            compact_session(s, None)

    def test_old_msgs_preserved_on_failure(self, ss):
        from claw.context.compaction import compact_session, CompactionError
        s = ss.create_session()
        for i in range(14):
            s.append_message("user" if i % 2 == 0 else "assistant", f"m{i}")
        n = len(s.messages)
        try:
            compact_session(s, None)
        except (CompactionError, AttributeError, TypeError):
            pass
        assert len(s.messages) == n


# ═════════════════════════════════════════════════════════════════════
# ToolRegistry
# ═════════════════════════════════════════════════════════════════════

class TestToolRegistry:
    def test_registered_names(self, reg):
        names = reg.list_tool_names()
        for n in ("current_time", "list_dir", "read_file"):
            assert n in names

    def test_execute_ok(self, reg):
        assert reg.execute_by_name("current_time", {}).ok

    def test_unknown_tool(self, reg):
        r = reg.execute_by_name("fake", {})
        assert not r.ok and "未知" in r.error

    def test_missing_required(self, reg):
        r = reg.execute_by_name("read_file", {})
        assert not r.ok and "缺少必需参数" in r.error

    def test_wrong_type(self, reg):
        r = reg.execute_by_name("read_file", {"path": 123})
        assert not r.ok and "类型错误" in r.error

    def test_definitions_format(self, reg):
        defs = reg.list_definitions()
        assert len(defs) >= 3
        for d in defs:
            assert d["type"] == "function"
            assert "name" in d["function"]


# ═════════════════════════════════════════════════════════════════════
# Workspace
# ═════════════════════════════════════════════════════════════════════

class TestWorkspace:
    def test_resolve_ok(self, wm, tmp):
        wm.set("s", str(tmp))
        assert str(wm.resolve("s", "x/y.txt")).startswith(str(tmp))

    def test_require_unset(self, wm):
        from claw.workspace.manager import WorkspaceError
        with pytest.raises(WorkspaceError):
            wm.resolve("no", "f.txt")

    def test_escape_rejected(self, wm, tmp):
        wm.set("s", str(tmp))
        from claw.workspace.manager import WorkspaceError
        with pytest.raises(WorkspaceError):
            wm.resolve("s", "../out.txt")

    def test_absolute_rejected(self, wm, tmp):
        wm.set("s", str(tmp))
        from claw.workspace.manager import WorkspaceError
        with pytest.raises(WorkspaceError):
            wm.resolve("s", "C:/Windows/test.txt")


# ═════════════════════════════════════════════════════════════════════
# Approval
# ═════════════════════════════════════════════════════════════════════

class TestApproval:
    def test_approve_flow(self, am):
        r = am.create("s1", "overwrite_file", {"path": "x"})
        assert r.status == "pending"
        d = am.approve(r.approval_id)
        assert d.status == "approved"

    def test_reject_flow(self, am):
        r = am.create("s1", "run_command", {"cmd": "del"})
        d = am.reject(r.approval_id, "no")
        assert d.status == "rejected" and d.reject_reason == "no"

    def test_pending_list(self, am):
        am.create("a", "t1", {}); am.create("b", "t2", {})
        assert len(am.get_pending()) == 2

    def test_session_filter(self, am):
        am.create("sa", "t1", {}); am.create("sb", "t2", {})
        assert len(am.list_by_session("sa")) == 1
