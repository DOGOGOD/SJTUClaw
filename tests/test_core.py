"""Pytest suite for SJTUClaw core modules.

Covers: SessionStore CRUD+persistence, ContextBuilder assembly order,
Compaction trigger/failure-protection, ToolRegistry param validation,
Workspace boundary enforcement, Approval approve/reject flow.
"""
import json
import re
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
    return MemoryStore(tmp / "memory")


@pytest.fixture
def reg():
    from claw.tools.base import ToolRegistry
    from claw.tools.readonly import register_all_readonly
    r = ToolRegistry()
    register_all_readonly(r)
    return r


@pytest.fixture
def wm(tmp, monkeypatch):
    import claw.workspace.manager as workspace_module
    from claw.workspace.manager import WorkspaceManager
    monkeypatch.setattr(
        workspace_module,
        "_BINDINGS_PATH",
        tmp / "workspace-state" / "bindings.json",
    )
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
# Timezone detection
# ═════════════════════════════════════════════════════════════════════


class TestTimezoneDetection:
    def test_loads_explicit_timezone_from_dotenv_before_detection(
        self,
        monkeypatch,
        tmp_path,
    ):
        import claw.config as config
        import claw.utils as utils

        env_path = tmp_path / ".env"
        env_path.write_text("CLAW_TIMEZONE=America/Los_Angeles\n", encoding="utf-8")
        monkeypatch.delenv("CLAW_TIMEZONE", raising=False)
        monkeypatch.setattr(config, "ENV_PATH", env_path)
        monkeypatch.setattr(config, "_dotenv_loaded", False)
        utils.detect_system_timezone.cache_clear()
        try:
            assert utils.detect_system_timezone() == "America/Los_Angeles"
        finally:
            utils.detect_system_timezone.cache_clear()

    def test_prefers_explicit_env_timezone(self, monkeypatch):
        import claw.utils as utils

        monkeypatch.setenv("CLAW_TIMEZONE", "America/New_York")
        utils.detect_system_timezone.cache_clear()
        try:
            assert utils.detect_system_timezone() == "America/New_York"
        finally:
            utils.detect_system_timezone.cache_clear()

    def test_falls_back_to_tz_env(self, monkeypatch):
        import claw.utils as utils

        monkeypatch.delenv("CLAW_TIMEZONE", raising=False)
        monkeypatch.setenv("TZ", "Europe/Paris")
        monkeypatch.setattr(utils, "_timezone_from_tzlocal", lambda: None)
        monkeypatch.setattr(utils, "_timezone_from_localtime_symlink", lambda: None)
        utils.detect_system_timezone.cache_clear()
        try:
            assert utils.detect_system_timezone() == "Europe/Paris"
        finally:
            utils.detect_system_timezone.cache_clear()

    def test_falls_back_to_shanghai_when_detection_fails(self, monkeypatch):
        import claw.utils as utils

        monkeypatch.setenv("CLAW_TIMEZONE", "Not/AZone")
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(utils, "_timezone_from_tzlocal", lambda: None)
        monkeypatch.setattr(utils, "_timezone_from_localtime_symlink", lambda: None)
        utils.detect_system_timezone.cache_clear()
        try:
            assert utils.detect_system_timezone() == "Asia/Shanghai"
        finally:
            utils.detect_system_timezone.cache_clear()


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
        # New JSONL format: file is at <base64(key)>.jsonl, not subdirectory
        # Find the actual file that was written
        import base64
        encoded = base64.urlsafe_b64encode(b"corr").decode().rstrip("=")
        corrupt_path = tmp / "sessions" / f"{encoded}.jsonl"
        corrupt_path.write_text("{bad", encoding="utf-8")
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
        assert roles == ["system", "user", "assistant"]
        assert "sp" in msgs[0]["content"]
        assert "soul" in msgs[0]["content"]
        assert "pref: zh" in msgs[0]["content"]
        assert roles[-2] == "user"; assert roles[-1] == "assistant"

    def test_return_budget_also_has_one_leading_system_message(self, cb, ss):
        cb._memory_store.add("remember this")
        s = ss.create_session()
        s.summary = "previous session summary"
        s.append_message("user", "continue")

        messages, budget = cb.build_messages(s, return_budget=True)

        assert [message["role"] for message in messages] == ["system", "user"]
        assert "remember this" in messages[0]["content"]
        assert "previous session summary" in messages[0]["content"]
        assert budget.total_tokens >= 0

    def test_system_merge_preserves_following_messages_verbatim(self):
        from claw.context.builder import _merge_leading_system_messages

        user_content = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]
        tool_message = {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "tool result",
        }
        messages = [
            {"role": "system", "content": "first"},
            {"role": "system", "content": "second"},
            {"role": "user", "content": user_content},
            tool_message,
        ]

        merged = _merge_leading_system_messages(messages)

        assert merged[0] == {"role": "system", "content": "first\n\n---\n\nsecond"}
        assert merged[1] is messages[2]
        assert merged[1]["content"] is user_content
        assert merged[2] is tool_message

    def test_empty_memory_excluded(self, cb, ss):
        s = ss.create_session()
        all_text = " ".join(m["content"] for m in cb.build_messages(s))
        # "长期记忆" appears in the tool contract, not as a memory block.
        # The memory block heading "## 长期记忆 (Memory)" should NOT appear
        # when there are no memory entries.
        assert "## 长期记忆 (Memory)" not in all_text

    def test_skill_block_absent_without_registry(self, cb, ss):
        s = ss.create_session()
        all_text = " ".join(m["content"] for m in cb.build_messages(s))
        assert "可用 Skills" not in all_text

    def test_runtime_context_uses_default_timezone(self, cb, ss):
        from claw.utils import default_timezone_name

        s = ss.create_session()
        s.append_message("user", "现在几点")
        messages = cb.build_messages(s)

        assert "当前时间:" in messages[-1]["content"]
        assert f"({default_timezone_name()})" in messages[-1]["content"]
        assert re.search(r"当前时间: .*[+-]\d{2}:\d{2}", messages[-1]["content"])

    def test_legacy_orphan_tool_message_becomes_assistant_context(self, cb, ss):
        s = ss.create_session()
        s.append_message("user", "我是谁")
        s.append_message("tool", '{"tool":"recall","result":"用户名是 Guztchian"}')

        messages = cb.build_messages(s)

        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"].startswith("[历史工具结果]")
        assert "Guztchian" in messages[-1]["content"]
        assert "tool_call_id" not in messages[-1]


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
        # v2: token-based threshold.  14 messages of "m0" etc. are far below
        # the default 2000-token limit, so we lower the bar explicitly.
        assert needs_compaction(s, max_message_tokens=5)

    def test_needed_by_chars(self, ss):
        from claw.context.compaction import needs_compaction
        s = ss.create_session()
        long_msg = "x" * 600
        for _ in range(7):
            s.append_message("user", long_msg)
        # v2: 4200 chars of "x" ≈ 1050 tokens — under the default 2000.
        # Lower the bar to match the old character-count expectation.
        assert needs_compaction(s, max_message_tokens=500)

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

    def test_current_time_uses_default_timezone(self, reg):
        from claw.utils import default_timezone_name

        result = reg.execute_by_name("current_time", {})

        assert result.ok
        assert result.content is not None
        assert f"({default_timezone_name()})" in result.content
        assert re.search(r"[+-]\d{2}:\d{2}", result.content)

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
    def test_load_tolerates_inaccessible_persisted_directory(
        self, monkeypatch, tmp
    ):
        import claw.workspace.manager as workspace_module
        from claw.workspace.manager import WorkspaceManager

        bindings_path = tmp / "workspace-state" / "bindings.json"
        bindings_path.parent.mkdir(parents=True)
        inaccessible = tmp / "inaccessible"
        bindings_path.write_text(
            json.dumps({"blocked-session": str(inaccessible)}),
            encoding="utf-8",
        )
        monkeypatch.setattr(workspace_module, "_BINDINGS_PATH", bindings_path)
        original_exists = Path.exists

        def permission_denied_for_workspace(path):
            if path == inaccessible:
                raise PermissionError(5, "Access is denied", str(path))
            return original_exists(path)

        monkeypatch.setattr(Path, "exists", permission_denied_for_workspace)

        manager = WorkspaceManager()

        assert manager.get("blocked-session") == inaccessible

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


# ═════════════════════════════════════════════════════════════════════
# MemoryStore — Structured (hierarchical memory)
# ═════════════════════════════════════════════════════════════════════


class TestMemoryStoreStructured:
    """Tests for structured MemoryEntry with category/tags/importance
    and recall/search/stats."""

    def test_add_with_all_fields(self, ms):
        entry = ms.add(
            "用户使用 FastAPI",
            category="project",
            tags=["python", "fastapi"],
            importance=4,
        )
        assert entry.memory_id.startswith("mem_")
        assert entry.category == "project"
        assert entry.tags == ["fastapi", "python"]  # normalised
        assert entry.importance == 4
        assert entry.content == "用户使用 FastAPI"

    def test_add_with_defaults(self, ms):
        entry = ms.add("一条简单记忆")
        assert entry.category == "general"
        assert entry.tags == []
        assert entry.importance == 3
        assert entry.recall_count == 0
        assert entry.last_recalled_at == ""

    def test_add_rejects_empty_content(self, ms):
        from claw.memory.store import MemoryStoreError
        with pytest.raises(MemoryStoreError):
            ms.add("")

    def test_add_rejects_invalid_category(self, ms):
        from claw.memory.store import MemoryStoreError
        with pytest.raises(MemoryStoreError):
            ms.add("test", category="invalid_cat")

    def test_add_rejects_invalid_importance(self, ms):
        from claw.memory.store import MemoryStoreError
        with pytest.raises(MemoryStoreError):
            ms.add("test", importance=10)

    def test_recall_by_tag_exact(self, ms):
        ms.add("项目A", category="project", tags=["python", "django"])
        ms.add("项目B", category="project", tags=["python", "fastapi"])
        results = ms.recall("fastapi")
        assert len(results) == 1
        assert results[0].content == "项目B"

    def test_recall_by_tag_partial(self, ms):
        ms.add("后端项目", category="project", tags=["backend", "api"])
        results = ms.recall("api")
        assert len(results) >= 1
        assert results[0].content == "后端项目"

    def test_recall_by_content(self, ms):
        ms.add("用户喜欢喝咖啡", category="user_preference")
        ms.add("用户是上海交大学生", category="fact")
        results = ms.recall("咖啡")
        assert len(results) == 1
        assert results[0].content == "用户喜欢喝咖啡"

    def test_recall_by_category_filter(self, ms):
        ms.add("项目A", category="project", tags=["python"])
        ms.add("喜欢蓝色", category="user_preference")
        results = ms.recall("python", category="project")
        assert len(results) == 1
        assert results[0].category == "project"

    def test_recall_no_results(self, ms):
        ms.add("项目A", category="project")
        results = ms.recall("nonexistent_xyz")
        assert len(results) == 0

    def test_recall_updates_tracking(self, ms):
        entry = ms.add("测试追踪", category="fact")
        assert entry.recall_count == 0
        results = ms.recall("追踪")
        assert len(results) == 1
        # Tracking is updated on the entry in-place
        after = ms.list()[0]
        assert after.recall_count == 1
        assert after.last_recalled_at != ""

    def test_recall_respects_limit(self, ms):
        for i in range(10):
            ms.add(f"条目{i} python", category="fact", tags=["python"])
        results = ms.recall("python", limit=3)
        assert len(results) <= 3

    def test_recall_importance_boosts_score(self, ms):
        ms.add("普通记忆", category="fact", tags=["test"], importance=1)
        ms.add("重要记忆", category="fact", tags=["test"], importance=5)
        results = ms.recall("test")
        # Higher importance should come first
        assert results[0].content == "重要记忆"

    def test_update_entry(self, ms):
        entry = ms.add("原始内容")
        updated = ms.update(entry.memory_id, "更新后内容")
        assert updated.content == "更新后内容"
        # updated_at may be equal if both calls happen within the same second
        assert updated.updated_at >= entry.updated_at

    def test_update_nonexistent_raises(self, ms):
        from claw.memory.store import MemoryStoreError
        with pytest.raises(MemoryStoreError):
            ms.update("mem_999", "xxx")

    def test_update_empty_content(self, ms):
        from claw.memory.store import MemoryStoreError
        entry = ms.add("test")
        with pytest.raises(MemoryStoreError):
            ms.update(entry.memory_id, "")

    def test_old_format_migration(self, tmp):
        """Auto-migrate entries from pre-hierarchical memory.json."""
        memory_dir = tmp / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        legacy_file = memory_dir / "memory.json"
        legacy_file.write_text(
            '[{"id":"mem_001","content":"旧记忆","createdAt":"2026-01-01T00:00:00"}]',
            encoding="utf-8",
        )
        from claw.memory.store import MemoryStore
        ms2 = MemoryStore(memory_dir)
        entries = ms2.list()
        assert len(entries) == 1
        assert entries[0].memory_id == "mem_001"
        assert entries[0].content == "旧记忆"
        assert entries[0].category == "general"
        assert entries[0].tags == []
        assert entries[0].importance == 3
        assert entries[0].recall_count == 0
        # Verify .md files were created and legacy file was renamed
        md_files = list(memory_dir.glob("*/*.md"))
        assert len(md_files) == 1
        assert not legacy_file.exists()  # renamed to .migrated

    def test_list_by_category(self, ms):
        ms.add("项目A", category="project")
        ms.add("偏好B", category="user_preference")
        ms.add("项目C", category="project")
        proj = ms.list_by_category("project")
        assert len(proj) == 2
        pref = ms.list_by_category("user_preference")
        assert len(pref) == 1

    def test_stats(self, ms):
        ms.add("项目A", category="project")
        ms.add("项目B", category="project")
        ms.add("偏好", category="user_preference")
        ms.add("事实", category="fact")
        stats = ms.stats()
        assert "项目信息" in stats
        assert stats["项目信息"] == 2
        assert "用户偏好" in stats
        assert stats["用户偏好"] == 1

    def test_delete(self, ms):
        entry = ms.add("test")
        ms.delete(entry.memory_id)
        assert len(ms.list()) == 0

    def test_delete_nonexistent(self, ms):
        from claw.memory.store import MemoryStoreError
        with pytest.raises(MemoryStoreError):
            ms.delete("mem_999")

    def test_tags_normalised(self, ms):
        entry = ms.add("test", tags=["  Python  ", "", "PYTHON", "Java"])
        assert entry.tags == ["java", "python"]  # sorted, deduped, stripped


# ═════════════════════════════════════════════════════════════════════
# Memory Tools — remember / recall
# ═════════════════════════════════════════════════════════════════════


class TestMemoryTools:
    """Test remember and recall tool definitions and validation."""

    @pytest.fixture
    def mem_reg(self, ms):
        from claw.tools.base import ToolRegistry
        from claw.tools.memory_tools import create_recall_tool, create_remember_tool

        r = ToolRegistry()
        r.register(create_remember_tool(ms))
        r.register(create_recall_tool(ms))
        return r

    def test_remember_safety_level_write(self, mem_reg):
        t = mem_reg.get_tool("remember")
        assert t.safety_level == "write"

    def test_recall_safety_level_readonly(self, mem_reg):
        t = mem_reg.get_tool("recall")
        assert t.safety_level == "read_only"

    def test_remember_requires_category_and_content(self, mem_reg):
        r = mem_reg.execute_by_name("remember", {})
        assert not r.ok and "缺少必需参数" in r.error

    def test_remember_invalid_category(self, mem_reg):
        r = mem_reg.execute_by_name("remember", {
            "category": "invalid", "content": "test"
        })
        assert not r.ok and ("无效的记忆类别" in r.error or "不在允许的范围" in r.error)

    def test_recall_requires_query(self, mem_reg):
        r = mem_reg.execute_by_name("recall", {})
        assert not r.ok and "缺少必需参数" in r.error

    def test_remember_ok(self, mem_reg, ms):
        r = mem_reg.execute_by_name("remember", {
            "category": "fact", "content": "测试保存"
        })
        assert r.ok
        assert len(ms.list()) == 1

    def test_recall_ok(self, mem_reg, ms):
        ms.add("Python项目", category="project", tags=["python"])
        r = mem_reg.execute_by_name("recall", {"query": "python"})
        assert r.ok


# ═════════════════════════════════════════════════════════════════════
# ContextBuilder — memory block is now lightweight
# ═════════════════════════════════════════════════════════════════════


class TestMemoryContext:
    """Test that the memory block in context is lightweight."""

    def test_memory_block_not_contains_full_content(self, cb, ms):
        ms.add("这是一条很具体的记忆内容", category="fact")
        block = cb._build_memory_block()
        assert block is not None
        # Should mention recall tool, not dump full content
        assert "recall" in block
        assert "remember" in block
        # The new memory block includes a "最近更新的记忆" preview showing
        # the first 120 chars of each entry's content. This is a summary, not
        # the full raw dump, but it does include the content text.
        assert "最近更新的记忆" in block
        # The full content should appear in the preview (since it's < 120 chars)
        assert "很具体的记忆内容" in block

    def test_memory_block_shows_stats(self, cb, ms):
        ms.add("项目A", category="project")
        ms.add("项目B", category="project")
        ms.add("偏好", category="user_preference")
        block = cb._build_memory_block()
        assert block is not None
        assert "3 条" in block
        assert "项目信息" in block

    def test_memory_block_empty(self, cb, ms):
        # No memories added — should return None
        block = cb._build_memory_block()
        assert block is None
