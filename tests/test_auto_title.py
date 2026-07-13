"""Tests for automatic session title generation.



Covers:

- generate_session_title: LLM summarisation of first user message

- auto_title_if_first_turn: full flow including session metadata and store

- Integration with CLI / Gateway entry points

"""



from __future__ import annotations



import pytest

from unittest.mock import MagicMock, patch



from claw.session.title import (

    generate_session_title,

    auto_title_if_first_turn,

    _MIN_MESSAGE_LEN,

    _MAX_TITLE_LEN,

)

from claw.session.store import SessionStore

from claw.session.models import Session





# ---------------------------------------------------------------------------

# generate_session_title

# ---------------------------------------------------------------------------





class _FakeLLMClient:

    """Minimal LLM client stub that returns a canned response."""



    def __init__(self, response: str = "项目结构分析"):

        self._response = response

        self.calls: list = []



    def chat(self, messages, budget=None):

        self.calls.append(list(messages))

        return self._response





class TestGenerateSessionTitle:

    """Unit tests for generate_session_title()."""



    def test_normal_message_returns_title(self):

        client = _FakeLLMClient("分析项目代码结构")

        title = generate_session_title("请帮我分析这个项目的代码结构", client)

        assert title == "分析项目代码结构"

        assert len(client.calls) == 1



    def test_strips_quotes_from_title(self):

        client = _FakeLLMClient('"代码审查"')

        title = generate_session_title("请审查我的代码", client)

        assert title == "代码审查"



    def test_strips_chinese_quotes(self):

        client = _FakeLLMClient("「数据库优化」")

        title = generate_session_title("帮我优化数据库查询", client)

        assert title == "数据库优化"



    def test_short_message_returns_none(self):

        client = _FakeLLMClient("标题")

        # Single character is below minimum

        title = generate_session_title("a", client)

        assert title is None



    def test_empty_message_returns_none(self):

        client = _FakeLLMClient("标题")

        title = generate_session_title("", client)

        assert title is None



    def test_whitespace_message_returns_none(self):

        client = _FakeLLMClient("标题")

        title = generate_session_title("   ", client)

        assert title is None



    def test_message_at_min_length_boundary(self):

        """Messages with exactly _MIN_MESSAGE_LEN chars should be titled."""

        client = _FakeLLMClient("测试")

        msg = "a" * _MIN_MESSAGE_LEN  # 2 chars

        title = generate_session_title(msg, client)

        assert title == "测试"



    def test_message_below_min_length_returns_none(self):

        client = _FakeLLMClient("测试")

        msg = "a" * (_MIN_MESSAGE_LEN - 1)  # 1 char

        title = generate_session_title(msg, client)

        assert title is None



    def test_llm_failure_returns_none(self):

        client = MagicMock()

        client.chat.side_effect = RuntimeError("API timeout")

        title = generate_session_title("请帮我分析这个项目的代码结构", client)

        assert title is None



    def test_empty_llm_response_returns_none(self):

        client = _FakeLLMClient("")

        title = generate_session_title("请帮我分析这个项目的代码结构", client)

        assert title is None



    def test_whitespace_llm_response_returns_none(self):

        client = _FakeLLMClient("   \n\t  ")

        title = generate_session_title("请帮我分析这个项目的代码结构", client)

        assert title is None



    def test_overlong_title_returns_none(self):

        client = _FakeLLMClient("a" * (_MAX_TITLE_LEN + 1))

        title = generate_session_title("请帮我分析这个项目的代码结构", client)

        assert title is None



    def test_title_at_max_length_boundary(self):

        client = _FakeLLMClient("a" * _MAX_TITLE_LEN)

        title = generate_session_title("请帮我分析这个项目的代码结构", client)

        assert title == "a" * _MAX_TITLE_LEN



    def test_long_message_truncated_for_prompt(self):

        """Very long first messages should be truncated to avoid burning tokens."""

        client = _FakeLLMClient("长消息测试")

        long_msg = "x" * 1000

        generate_session_title(long_msg, client)

        # Verify the LLM was called

        assert len(client.calls) == 1

        # The prompt sent to LLM should be truncated

        user_msg = client.calls[0][1]["content"]

        assert len(user_msg) <= 500





# ---------------------------------------------------------------------------

# auto_title_if_first_turn

# ---------------------------------------------------------------------------





class TestAutoTitleIfFirstTurn:

    """Tests for auto_title_if_first_turn() with a real SessionStore."""



    @pytest.fixture

    def store(self, tmp_path):

        return SessionStore(tmp_path / "sessions")



    @pytest.fixture

    def llm_client(self):

        return _FakeLLMClient("项目结构分析")



    def test_first_turn_generates_title(self, store, llm_client):

        session = store.create_session(session_id="s1")

        session.append_message("user", "请帮我分析这个项目的代码结构")

        store.save(session)



        messages = [{"role": "user", "content": "请帮我分析这个项目的代码结构"}]

        title = auto_title_if_first_turn("s1", messages, store, llm_client)

        assert title == "项目结构分析"



        # Verify the title was persisted

        updated = store.get("s1")

        assert updated.title == "项目结构分析"



    def test_multiple_user_messages_skips(self, store, llm_client):

        session = store.create_session(session_id="s2")

        session.append_message("user", "第一条消息")

        session.append_message("assistant", "回复")

        session.append_message("user", "第二条消息")

        store.save(session)



        messages = [

            {"role": "user", "content": "第一条消息"},

            {"role": "assistant", "content": "回复"},

            {"role": "user", "content": "第二条消息"},

        ]

        title = auto_title_if_first_turn("s2", messages, store, llm_client)

        assert title is None



    def test_zero_user_messages_skips(self, store, llm_client):

        session = store.create_session(session_id="s3")

        session.append_message("assistant", "系统初始化")

        store.save(session)



        messages = [{"role": "assistant", "content": "系统初始化"}]

        title = auto_title_if_first_turn("s3", messages, store, llm_client)

        assert title is None



    def test_user_edited_title_not_overwritten(self, store, llm_client):

        session = store.create_session(session_id="s4", title="自定义标题")

        session.metadata["title_user_edited"] = True

        session.append_message("user", "请帮我分析这个项目的代码结构")

        store.save(session)



        messages = [{"role": "user", "content": "请帮我分析这个项目的代码结构"}]

        title = auto_title_if_first_turn("s4", messages, store, llm_client)

        assert title is None



        # Original title preserved

        assert store.get("s4").title == "自定义标题"



    def test_nonexistent_session_returns_none(self, store, llm_client):

        messages = [{"role": "user", "content": "hello"}]

        title = auto_title_if_first_turn("nonexistent", messages, store, llm_client)

        assert title is None



    def test_short_first_message_returns_none(self, store, llm_client):
        session = store.create_session(session_id="s5")
        # Single character is below minimum (2)
        session.append_message("user", "a")
        store.save(session)

        messages = [{"role": "user", "content": "a"}]
        title = auto_title_if_first_turn("s5", messages, store, llm_client)
        assert title is None



    def test_llm_failure_returns_none_without_crash(self, store):

        session = store.create_session(session_id="s6")

        session.append_message("user", "请帮我分析这个项目的代码结构")

        store.save(session)



        client = MagicMock()

        client.chat.side_effect = RuntimeError("API error")



        messages = [{"role": "user", "content": "请帮我分析这个项目的代码结构"}]

        title = auto_title_if_first_turn("s6", messages, store, client)

        assert title is None



    def test_rename_failure_returns_none(self, tmp_path, llm_client):

        store = SessionStore(tmp_path / "sessions")

        session = store.create_session(session_id="s7")

        session.append_message("user", "请帮我分析这个项目的代码结构")

        store.save(session)



        # Mock rename to fail

        store.rename = MagicMock(side_effect=RuntimeError("disk full"))



        messages = [{"role": "user", "content": "请帮我分析这个项目的代码结构"}]

        title = auto_title_if_first_turn("s7", messages, store, llm_client)

        assert title is None



    def test_idempotent_on_second_call(self, store, llm_client):

        """After the first auto-title, a second call should not overwrite."""

        session = store.create_session(session_id="s8")

        session.append_message("user", "请帮我分析这个项目的代码结构")

        store.save(session)



        messages = [{"role": "user", "content": "请帮我分析这个项目的代码结构"}]



        # First call generates the title

        title1 = auto_title_if_first_turn("s8", messages, store, llm_client)

        assert title1 == "项目结构分析"



        # Simulate second turn: user sends another message + assistant replies

        session = store.get("s8")

        session.append_message("assistant", "好的，我来分析")

        session.append_message("user", "请继续详细分析")

        session.append_message("assistant", "好的，我继续...")

        store.save(session)



        messages2 = [

            {"role": "user", "content": "请帮我分析这个项目的代码结构"},

            {"role": "assistant", "content": "好的，我来分析"},

            {"role": "user", "content": "请继续详细分析"},

            {"role": "assistant", "content": "好的，我继续..."},

        ]

        title2 = auto_title_if_first_turn("s8", messages2, store, llm_client)

        assert title2 is None  # No longer first turn (2 user messages)



    def test_title_with_special_characters_stripped(self, store):

        """LLM output with surrounding quotes should be cleaned."""

        client = _FakeLLMClient("《数据库设计》")

        session = store.create_session(session_id="s9")

        session.append_message("user", "帮我设计数据库")

        store.save(session)



        messages = [{"role": "user", "content": "帮我设计数据库"}]

        title = auto_title_if_first_turn("s9", messages, store, client)

        assert title == "数据库设计"





# ---------------------------------------------------------------------------

# Integration: title is persisted to disk

# ---------------------------------------------------------------------------





class TestTitlePersistence:

    """Verify that auto-generated titles survive a store reload."""



    def test_title_survives_reload(self, tmp_path):

        store = SessionStore(tmp_path / "sessions")

        client = _FakeLLMClient("持久化测试标题")



        session = store.create_session(session_id="persist1")

        session.append_message("user", "请帮我分析这个项目的代码结构")

        store.save(session)



        messages = [{"role": "user", "content": "请帮我分析这个项目的代码结构"}]

        auto_title_if_first_turn("persist1", messages, store, client)



        # Reload store from disk

        store2 = SessionStore(tmp_path / "sessions")

        reloaded = store2.get("persist1")

        assert reloaded.title == "持久化测试标题"

