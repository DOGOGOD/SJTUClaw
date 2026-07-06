"""Compaction: summarize older session messages into `session.summary`.

Boundary (Step 4 requirement): compaction only ever reads/writes
`session.summary` and `session.messages`. It builds its own minimal,
dedicated LLM request and never touches the app's system prompt, soul
or memory store — those are wired in independently by
`claw.context.builder.ContextBuilder`.

Trigger thresholds (approximate, not exact token counts):

- `KEEP_RECENT_MESSAGES` (6): the most recent 6 raw messages (about the
  last 3 user/assistant exchanges) are always kept verbatim, so the
  latest exchange is never summarized away.
- `MAX_MESSAGES_BEFORE_COMPACTION` (12): trigger once the session has
  more than 12 messages, regardless of their length. This is a simple,
  interpretable safety net against sessions with many short messages.
- `MAX_CHARS_BEFORE_COMPACTION` (4000): trigger once the total
  character count of all message content exceeds 4000. Character
  count is used as a cheap, model-agnostic proxy for token count
  (exact tokenization depends on the model/tokenizer, which claw does
  not have access to). For mixed CN/EN text, 1 token is roughly
  1.5-2 characters, so 4000 characters stays a comfortable fraction of
  typical 8K+ token context windows, leaving room for stable context
  (system prompt/soul/memory) and the model's reply.

Either condition alone is enough to trigger compaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from claw.llm.client import LLMClient, LLMError
from claw.session.models import Message, Session
from claw.session.store import SessionStore, SessionStoreError

KEEP_RECENT_MESSAGES = 6
MAX_MESSAGES_BEFORE_COMPACTION = 12
MAX_CHARS_BEFORE_COMPACTION = 4000

_COMPACTION_SYSTEM_INSTRUCTION = (
    "你是一个专门负责压缩对话历史的摘要助手。\n"
    "你会收到一份可能为空的已有摘要，以及一段较早的对话消息（更早的用户/助手轮次）。\n"
    "请把它们合并成一份新的摘要，用于后续继续对话时提供上下文，而不是重新讲述整个对话。\n\n"
    "摘要必须保留：\n"
    "- 当前任务\n"
    "- 已经完成的内容\n"
    "- 用户明确提出的要求、偏好和约束\n"
    "- 尚未解决的问题\n"
    "- 影响后续回答的关键事实\n\n"
    "摘要必须删除：\n"
    "- 寒暄\n"
    "- 重复表达\n"
    "- 无关细节\n"
    "- 没有继续使用价值的中间过程\n\n"
    "不需要固定格式，但必须简洁、可读，适合直接作为后续对话的上下文。"
    "只输出摘要正文本身，不要输出多余的说明、标题或前后缀。"
)


class CompactionError(RuntimeError):
    """Raised when computing a compaction summary fails.

    Whenever this is raised, `session` has not been modified: old
    messages are always preserved unless a valid new summary was
    successfully computed.
    """


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of successfully compacting a session's older messages."""

    old_message_count: int
    recent_message_count: int
    summary: str


@dataclass(frozen=True)
class CompactionOutcome:
    """Result of `compact_and_persist`.

    `save_error` is None on a clean save; otherwise it holds a
    human-readable message explaining why the (already-applied) result
    might not have been persisted to disk.
    """

    result: CompactionResult
    save_error: str | None


def needs_compaction(session: Session) -> bool:
    """Return True if `session` has enough old messages to compact."""
    if len(session.messages) <= KEEP_RECENT_MESSAGES:
        return False
    total_chars = sum(len(message.content) for message in session.messages)
    return (
        len(session.messages) > MAX_MESSAGES_BEFORE_COMPACTION
        or total_chars > MAX_CHARS_BEFORE_COMPACTION
    )


def compact_session(session: Session, llm_client: LLMClient) -> CompactionResult:
    """Compute a new merged summary for `session`'s older messages.

    This does NOT mutate `session`. Callers must only apply the result
    (see `apply_compaction_result`) after this call succeeds, so that a
    failure here never loses the original messages.

    Raises:
        CompactionError: if there is nothing old enough to compact, the
            LLM call fails, or the LLM returns an empty/invalid summary.
    """
    messages = session.messages
    if len(messages) <= KEEP_RECENT_MESSAGES:
        raise CompactionError(
            f"当前 session 只有 {len(messages)} 条消息，"
            f"不超过保留窗口（{KEEP_RECENT_MESSAGES}），无需压缩。"
        )

    split_index = len(messages) - KEEP_RECENT_MESSAGES
    old_messages = messages[:split_index]
    recent_messages = messages[split_index:]

    request_messages = _build_compaction_request(session.summary, old_messages)

    try:
        raw_summary = llm_client.chat(request_messages)
    except LLMError as exc:
        raise CompactionError(
            f"压缩失败：调用 LLM 生成摘要时出错，原始消息未被修改。详情：{exc}"
        ) from exc

    new_summary = (raw_summary or "").strip()
    if not new_summary:
        raise CompactionError(
            "压缩失败：LLM 返回的摘要为空，已放弃本次压缩，原始消息未被修改。"
        )

    return CompactionResult(
        old_message_count=len(old_messages),
        recent_message_count=len(recent_messages),
        summary=new_summary,
    )


def apply_compaction_result(session: Session, result: CompactionResult) -> None:
    """Mutate `session` to apply an already-computed `CompactionResult`.

    Only call this after `compact_session` has returned successfully.
    """
    session.messages = session.messages[result.old_message_count :]
    session.summary = result.summary
    session.touch()


def compact_and_persist(
    session: Session, session_store: SessionStore, llm_client: LLMClient
) -> CompactionOutcome:
    """Compact `session`, apply the result, and try to persist it.

    Raises:
        CompactionError: computing the summary failed; `session` is
            left completely untouched (old messages preserved).

    On success, `session` is mutated in place. If saving to disk then
    fails, the in-memory session still reflects the new summary/trimmed
    messages, but `CompactionOutcome.save_error` is set so the caller
    can warn the user that a restart might lose this specific result.
    """
    result = compact_session(session, llm_client)
    apply_compaction_result(session, result)

    save_error: str | None = None
    try:
        session_store.save(session)
    except SessionStoreError as exc:
        save_error = str(exc)

    return CompactionOutcome(result=result, save_error=save_error)


def _build_compaction_request(
    existing_summary: str, old_messages: list[Message]
) -> list[dict[str, str]]:
    if existing_summary.strip():
        summary_block = f"已有摘要：\n{existing_summary.strip()}"
    else:
        summary_block = "已有摘要：（无）"

    transcript_lines = [f"{m.role}: {m.content}" for m in old_messages]
    transcript_block = "需要合并进摘要的较早对话：\n" + "\n".join(transcript_lines)

    user_content = f"{summary_block}\n\n{transcript_block}"
    return [
        {"role": "system", "content": _COMPACTION_SYSTEM_INSTRUCTION},
        {"role": "user", "content": user_content},
    ]
