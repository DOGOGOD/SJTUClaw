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
