"""Tests for compaction v2: token counting, budget, and async worker."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


class TestTokenCounter:
    def test_empty_string(self):
        from claw.context.token_counter import count_tokens
        assert count_tokens("") == 0

    def test_english_text(self):
        from claw.context.token_counter import count_tokens
        # "hello world" should produce a reasonable token count
        tokens = count_tokens("hello world")
        assert tokens >= 2  # at minimum two words are two tokens

    def test_chinese_text(self):
        from claw.context.token_counter import count_tokens
        tokens = count_tokens("你好世界")
        assert tokens >= 2  # Chinese chars should never be 0

    def test_mixed_text(self):
        from claw.context.token_counter import count_tokens
        tokens = count_tokens("你好hello")
        assert tokens >= 2

    def test_count_for_messages(self):
        from claw.context.token_counter import count_tokens_for_messages
        from claw.session.models import Message

        msgs = [
            Message(role="user", content="hello"),
            Message(role="assistant", content="world"),
        ]
        tokens = count_tokens_for_messages(msgs)
        # role strings are not counted, only content
        assert tokens >= 2


# ---------------------------------------------------------------------------
# ContextBudget
# ---------------------------------------------------------------------------


class TestContextBudget:
    def test_basic_measure(self):
        from claw.context.budget import ContextBudget
        from claw.session.models import Message

        budget = ContextBudget.measure(
            max_tokens=1000,
            system_prompt="You are helpful.",
            soul="Be concise.",
            messages=[Message(role="user", content="hi")],
        )
        assert budget.max_tokens == 1000
        assert budget.total_tokens > 0
        assert budget.available_tokens < 1000
        assert 0.0 < budget.usage_ratio < 1.0

    def test_usage_ratio(self):
        from claw.context.budget import ContextBudget

        # Use diverse CJK text that reliably tokenises to many tokens
        # regardless of whether tiktoken or the fallback heuristic is active.
        budget = ContextBudget.measure(
            max_tokens=100,
            system_prompt="今天天气真好我们去公园散步吧看花开花落云卷云舒" * 30,
        )
        assert budget.usage_ratio > 1.0
        assert budget.available_tokens < 0

    def test_overflow_raises(self):
        from claw.context.budget import ContextBudget, ContextOverflowError

        # Construct a budget that is at > 105% usage with diverse text
        budget = ContextBudget.measure(
            max_tokens=100,
            system_prompt="今天天气真好我们去公园散步吧看花开花落云卷云舒" * 50,
        )
        with pytest.raises(ContextOverflowError):
            budget.check_overflow()

    def test_overflow_warns_not_raises_below_105(self):
        from claw.context.budget import ContextBudget

        # Just barely over 100%, but under 105% — should not raise
        budget = ContextBudget.measure(
            max_tokens=100,
            system_prompt="x" * 410,  # ~102-103 tokens → ~102-103%
        )
        # Should not raise
        budget.check_overflow()

    def test_fixed_overhead(self):
        from claw.context.budget import ContextBudget

        budget = ContextBudget.measure(
            max_tokens=10000,
            system_prompt="sys",
            soul="soul",
            memory_block="memory",
            tool_defs_text="tools",
            skill_block="skills",
            summary_block="summary",
        )
        overhead = budget.fixed_overhead_tokens
        assert overhead > 0
        assert budget.messages_tokens == 0
        assert budget.total_tokens == overhead


# ---------------------------------------------------------------------------
# Compaction (v2 thresholds)
# ---------------------------------------------------------------------------


class TestCompactionV2:
    def test_summary_and_boundary_survive_store_roundtrip(self, tmp_path):
        from claw.session.store import SessionStore

        store = SessionStore(tmp_path / "sessions")
        session = store.create_session(session_id="compact-roundtrip")
        for index in range(6):
            session.append_message("user" if index % 2 == 0 else "assistant", f"message-{index}")
        session.summary = "durable compact summary"
        session.last_consolidated = 2
        store.save(session)

        loaded = SessionStore(tmp_path / "sessions").get("compact-roundtrip")
        assert loaded.summary == "durable compact summary"
        assert loaded.last_consolidated == 2
        assert len(loaded.messages) == 6

    def test_compact_and_persist_keeps_summary_after_restart(self, tmp_path):
        from claw.context.compaction import compact_and_persist
        from claw.session.store import SessionStore

        class SummaryLLM:
            def chat(self, _messages):
                return "SUMMARY_FROM_LLM"

        store = SessionStore(tmp_path / "sessions")
        session = store.create_session(session_id="actual-compact")
        for index in range(10):
            role = "user" if index % 2 == 0 else "assistant"
            session.append_message(role, (f"message-{index} unique content " * 300))
        store.save(session)

        outcome = compact_and_persist(session, store, SummaryLLM())
        assert outcome.save_error is None
        assert session.last_consolidated > 0

        loaded = SessionStore(tmp_path / "sessions").get("actual-compact")
        assert loaded.summary == "SUMMARY_FROM_LLM"
        assert loaded.last_consolidated == session.last_consolidated
        assert len(loaded.messages) == 10

    def test_manual_compact_forces_short_history_before_auto_threshold(self, tmp_path):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.context.compaction import needs_compaction
        from claw.memory.store import MemoryStore
        from claw.session.store import SessionStore

        class SummaryLLM:
            def chat(self, _messages):
                return "MANUAL_SUMMARY"

        store = SessionStore(tmp_path / "sessions")
        session = store.create_session(session_id="manual-compact")
        for index in range(6):
            role = "user" if index % 2 == 0 else "assistant"
            session.append_message(role, f"short-{index}")
        store.save(session)

        assert not needs_compaction(session)

        state = RuntimeState(
            session_store=store,
            memory_store=MemoryStore(tmp_path / "memory"),
            llm_client=SummaryLLM(),
            current_session_id=session.session_id,
        )
        result = handle_command("/compact", state)

        assert result.startswith("压缩简报")
        assert "摘要预览：\nMANUAL_SUMMARY" in result
        assert "未截断正在进行的任务" in result
        assert "Compacted session manual-compact." in result
        assert "Old messages: 2" in result
        loaded = store.get(session.session_id)
        assert loaded.summary == "MANUAL_SUMMARY"
        assert loaded.last_consolidated == 2
        assert len(loaded.messages) == 6

    def test_manual_compact_does_not_race_background_worker(self, tmp_path):
        from claw.cli.commands import RuntimeState, handle_command
        from claw.memory.store import MemoryStore
        from claw.session.store import SessionStore

        class BusyWorker:
            def is_running(self):
                return True

        store = SessionStore(tmp_path / "sessions")
        session = store.create_session(session_id="busy-compact")
        for index in range(6):
            session.append_message(
                "user" if index % 2 == 0 else "assistant",
                f"short-{index}",
            )
        store.save(session)

        state = RuntimeState(
            session_store=store,
            memory_store=MemoryStore(tmp_path / "memory"),
            llm_client=object(),
            current_session_id=session.session_id,
            compaction_worker=BusyWorker(),
        )
        result = handle_command("/compact", state)

        assert "后台压缩正在进行" in result
        loaded = store.get(session.session_id)
        assert loaded.summary == ""
        assert loaded.last_consolidated == 0

    def test_missing_legacy_summary_reexposes_raw_transcript(self, tmp_path):
        from claw.session.store import SessionStore

        store = SessionStore(tmp_path / "sessions")
        session = store.create_session(session_id="legacy-compact")
        for index in range(6):
            session.append_message("user" if index % 2 == 0 else "assistant", f"message-{index}")
        session.last_consolidated = 2
        store.save(session)

        # Simulate the old writer, which stored the boundary without summary.
        path = store._key_path("legacy-compact")
        lines = path.read_text("utf-8").splitlines()
        import json
        metadata = json.loads(lines[0])
        metadata["metadata"].pop("summary", None)
        lines[0] = json.dumps(metadata, ensure_ascii=False)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        loaded = SessionStore(tmp_path / "sessions").get("legacy-compact")
        assert loaded.summary == ""
        assert loaded.last_consolidated == 0
        assert len(loaded.messages) == 6

    def test_needs_compaction_token_threshold(self, ss):
        from claw.context.compaction import needs_compaction

        s = ss.create_session()
        # Create a long message that exceeds the token limit
        s.append_message("user", "长消息" * 500)
        s.append_message("assistant", "收到")
        s.append_message("user", "继续" * 500)
        s.append_message("assistant", "好的")
        s.append_message("user", "再来" * 500)
        # Total tokens should far exceed default 2000
        assert needs_compaction(s)

    def test_needs_compaction_short_messages_no_trigger(self, ss):
        from claw.context.compaction import needs_compaction

        s = ss.create_session()
        for i in range(10):
            s.append_message("user" if i % 2 == 0 else "assistant", f"msg{i}")
        # 10 short messages should be below 2000 token threshold
        assert not needs_compaction(s)

    def test_needs_compaction_min_messages_floor(self, ss):
        from claw.context.compaction import needs_compaction

        s = ss.create_session()
        # Only 3 messages — below KEEP_RECENT_MESSAGES_MIN=4
        s.append_message("user", "x" * 5000)
        s.append_message("assistant", "x" * 5000)
        s.append_message("user", "x" * 5000)
        assert not needs_compaction(s)

    def test_worker_submits_only_after_token_threshold(self, ss, monkeypatch):
        from claw.config import CompactionConfig
        from claw.context.compaction_worker import CompactionWorker

        class NoopLLM:
            pass

        worker = CompactionWorker(
            NoopLLM(),
            ss,
            config=CompactionConfig(
                max_message_tokens=5,
                keep_recent_tokens=5,
            ),
        )
        submitted = []
        monkeypatch.setattr(
            worker,
            "submit",
            lambda session: submitted.append(session.session_id) or True,
        )

        short = ss.create_session(session_id="below-token-threshold")
        for index in range(6):
            short.append_message(
                "user" if index % 2 == 0 else "assistant",
                "",
            )
        assert not worker.submit_if_needed(short)

        long = ss.create_session(session_id="above-token-threshold")
        for index in range(6):
            long.append_message(
                "user" if index % 2 == 0 else "assistant",
                "token rich message",
            )
        assert worker.submit_if_needed(long)
        assert submitted == ["above-token-threshold"]

    def test_worker_skips_threshold_tail_without_safe_prefix(self, ss):
        from claw.config import CompactionConfig
        from claw.context.compaction_worker import CompactionWorker

        class UnexpectedLLM:
            def chat(self, _messages):
                raise AssertionError("no-op compaction must not call the LLM")

        worker = CompactionWorker(
            UnexpectedLLM(),
            ss,
            config=CompactionConfig(
                max_message_tokens=5,
                keep_recent_tokens=1000,
                keep_recent_messages_min=4,
            ),
        )
        session = ss.create_session(session_id="no-safe-prefix")
        session.append_message("user", "oldest message " * 500)
        session.append_message("assistant", "a")
        session.append_message("user", "b")
        session.append_message("assistant", "c")
        session.append_message("user", "d")

        assert not worker.submit_if_needed(session)
        assert not worker.is_running()

    def test_background_noop_is_silent_and_not_retried(self, ss, capsys):
        from claw.config import CompactionConfig
        from claw.context.compaction_worker import CompactionWorker

        class CountingLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, _messages):
                self.calls += 1
                return "UNEXPECTED"

        llm = CountingLLM()
        worker = CompactionWorker(
            llm,
            ss,
            config=CompactionConfig(
                keep_recent_tokens=1000,
                keep_recent_messages_min=4,
            ),
        )
        session = ss.create_session(session_id="silent-noop")
        for index in range(6):
            session.append_message(
                "user" if index % 2 == 0 else "assistant",
                f"short-{index}",
            )

        worker._do_compact(
            session,
            list(session.messages),
            session.summary,
            session.revision,
        )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert llm.calls == 0
        assert session.last_consolidated == 0

    def test_worker_reports_success_after_persist(self, ss):
        from claw.config import CompactionConfig
        from claw.context.compaction_worker import CompactionWorker

        class SummaryLLM:
            def chat(self, _messages):
                return "AUTO_SUMMARY"

        completed = []
        worker = CompactionWorker(
            SummaryLLM(),
            ss,
            config=CompactionConfig(
                keep_recent_tokens=20,
                keep_recent_messages_min=2,
            ),
            on_complete=lambda session, result: completed.append(
                (
                    session.session_id,
                    result.old_message_count,
                    result.recent_message_count,
                    result.summary,
                )
            ),
        )
        session = ss.create_session(session_id="auto-complete-notice")
        for index in range(8):
            session.append_message(
                "user" if index % 2 == 0 else "assistant",
                f"message {index} " * 20,
            )
        ss.save(session)

        assert worker.submit(session)
        assert worker.wait(timeout=5)
        assert completed == [
            (
                "auto-complete-notice",
                session.last_consolidated,
                2,
                "AUTO_SUMMARY",
            )
        ]
        assert ss.get(session.session_id).summary == "AUTO_SUMMARY"

    def test_ui_only_notices_are_not_sent_to_summary_llm(self, ss):
        from claw.context.compaction import compact_session

        class RecordingLLM:
            def __init__(self):
                self.messages = []

            def chat(self, messages):
                self.messages = messages
                return "SUMMARY_WITHOUT_NOTICE"

        llm = RecordingLLM()
        session = ss.create_session(session_id="skip-compaction-notice")
        session.append_message(
            "assistant",
            "自动压缩已完成 SHOULD_NOT_REACH_LLM",
            _command=True,
            injected_event="compaction_notice",
        )
        for index in range(6):
            session.append_message(
                "user" if index % 2 == 0 else "assistant",
                f"conversation-{index}",
            )

        result = compact_session(
            session,
            llm,
            keep_recent_messages_min=2,
            force=True,
        )

        assert result.summary == "SUMMARY_WITHOUT_NOTICE"
        serialized_request = "\n".join(
            message["content"] for message in llm.messages
        )
        assert "SHOULD_NOT_REACH_LLM" not in serialized_request

    def test_split_by_tokens(self):
        from claw.context.compaction import _find_split_index
        from claw.session.models import Message

        # Use diverse English text that reliably produces many tokens
        # regardless of tiktoken vs fallback heuristic.
        long_text = "The quick brown fox jumps over the lazy dog. " * 200

        msgs = [
            Message(role="user", content="short"),
            Message(role="assistant", content="reply"),
            Message(role="user", content=long_text),   # many tokens
            Message(role="assistant", content=long_text),  # many tokens
        ]
        # keep_tokens=100: only the last msg crosses the threshold
        split_small = _find_split_index(msgs, keep_tokens=100)
        # The last message alone (long_text) crosses 100 tokens, so split at 3
        assert split_small == 3
        assert split_small > 0

        # keep_tokens very high: nothing to compact
        split_all = _find_split_index(msgs, keep_tokens=999999)
        assert split_all == 0

    def test_compact_session_fails_few_msgs(self, ss):
        from claw.context.compaction import CompactionError, compact_session

        s = ss.create_session()
        s.append_message("user", "hi")
        s.append_message("assistant", "ok")
        with pytest.raises(CompactionError):
            compact_session(s, None)

    def test_old_msgs_preserved_on_failure(self, ss):
        from claw.context.compaction import CompactionError, compact_session

        s = ss.create_session()
        # Enough messages but LLM client is None → should fail
        for _ in range(6):
            s.append_message("user", "长消息内容" * 200)
            s.append_message("assistant", "收到回复" * 200)
        n = len(s.messages)
        try:
            compact_session(s, None)  # type: ignore[arg-type]
        except (CompactionError, AttributeError, TypeError):
            pass
        assert len(s.messages) == n


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ss(tmp_path):
    from claw.session.store import SessionStore
    return SessionStore(tmp_path)
