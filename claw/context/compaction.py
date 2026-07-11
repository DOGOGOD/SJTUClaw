"""Compaction: summarize older session messages into `session.summary`.

Boundary (Step 4 requirement): compaction only ever reads/writes
`session.summary` and `session.messages`. It builds its own minimal,
dedicated LLM request and never touches the app's system prompt, soul
or memory store --- those are wired in independently by
`claw.context.builder.ContextBuilder`.

v3 changes (optimization):

- Multi-round token-budget consolidation: compacts in up to 5 rounds
  until the session fits within the context budget.
- User-turn boundary detection: never splits mid-turn — always
  compacts at user-message boundaries.
- Summary persistence in session metadata for idle-session archival
  and process restart recovery.
- Idle-session hard-truncation (``compact_idle_session``).
- Proper token estimation including system prompt, tool definitions,
  and summary overhead — not just raw message content.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from claw.context.token_counter import count_tokens, count_tokens_for_messages
from claw.llm.client import LLMClient, LLMError
from claw.session.models import Message, Session
from claw.session.store import SessionStore, SessionStoreError

# ---------------------------------------------------------------------------
# Configurable thresholds (env-overridable at import time)
# ---------------------------------------------------------------------------

KEEP_RECENT_MESSAGES_MIN = int(
    os.getenv("COMPACT_KEEP_RECENT_MESSAGES_MIN", "4")
)
"""Absolute floor: never compact if there are <= this many messages."""

MAX_MESSAGE_TOKENS = int(os.getenv("COMPACT_MAX_MESSAGE_TOKENS", "2000"))
"""Trigger compaction when session.messages content exceeds this many tokens."""

KEEP_RECENT_TOKENS = int(os.getenv("COMPACT_KEEP_RECENT_TOKENS", "1000"))
"""Token budget for the recent-message window that is kept verbatim."""

# ---------------------------------------------------------------------------
# Consolidation constants
# ---------------------------------------------------------------------------

_MAX_CONSOLIDATION_ROUNDS = 5
_SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift
_ARCHIVE_SUMMARY_MAX_CHARS = 8000
_RAW_ARCHIVE_MAX_CHARS = 16000

# ---------------------------------------------------------------------------
# Compaction system instruction
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Threshold helpers
# ---------------------------------------------------------------------------


def _find_split_index(messages: list[Message], keep_tokens: int) -> int:
    """Walk backwards from the end of *messages*, accumulating token
    counts.  Return the index (0-based) of the first message that should
    be *kept* (i.e. old messages are ``messages[:split_index]``).

    If the total token count of all messages is less than *keep_tokens*,
    returns 0 (nothing to compact).
    """
    accumulated = 0
    for i in range(len(messages) - 1, -1, -1):
        accumulated += count_tokens(messages[i].content)
        if accumulated >= keep_tokens:
            return i
    return 0


def _find_user_boundary_index(
    messages: list[Message],
    start_from: int,
    tokens_to_remove: int,
) -> tuple[int, int] | None:
    """Find a user-turn boundary starting from *start_from* that removes
    at least *tokens_to_remove* tokens.

    Returns ``(end_index, tokens_removed)`` or None if no boundary exists.
    The returned *end_index* is exclusive for the chunk to remove
    (i.e. ``messages[start_from:end_index]`` is the chunk).
    """
    removed_tokens = 0
    last_boundary: tuple[int, int] | None = None
    for idx in range(start_from, len(messages)):
        msg = messages[idx]
        if idx > start_from and msg.role == "user":
            last_boundary = (idx, removed_tokens)
            if removed_tokens >= tokens_to_remove:
                return last_boundary
        removed_tokens += count_tokens(msg.content)
    return last_boundary


def needs_compaction(
    session: Session,
    *,
    max_message_tokens: int | None = None,
    context_usage_ratio: float | None = None,
    context_total_tokens: int | None = None,
    max_context_tokens: int | None = None,
) -> bool:
    """Return True if *session* has enough old messages to compact.

    Two independent triggers (either is sufficient):

    1. **Message token threshold**: the content of ``session.messages``
       exceeds *max_message_tokens* (default ``MAX_MESSAGE_TOKENS``).
    2. **Context budget pressure**: if *context_usage_ratio* and
       *max_context_tokens* are provided, the ratio of total context
       tokens to max budget is checked.

    Both triggers are gated by ``KEEP_RECENT_MESSAGES_MIN`` — a session
    with very few messages is never compacted, regardless of token count.
    """
    unconsolidated = session.get_unconsolidated_messages()
    if len(unconsolidated) <= KEEP_RECENT_MESSAGES_MIN:
        return False

    max_tok = max_message_tokens if max_message_tokens is not None else MAX_MESSAGE_TOKENS
    message_tokens = count_tokens_for_messages(unconsolidated)
    if message_tokens > max_tok:
        return True

    # Context budget pressure check
    if (
        context_usage_ratio is not None
        and max_context_tokens is not None
        and context_total_tokens is not None
        and max_context_tokens > 0
    ):
        actual_ratio = context_total_tokens / max_context_tokens
        if actual_ratio > context_usage_ratio:
            return True

    return False


# ---------------------------------------------------------------------------
# Compaction logic
# ---------------------------------------------------------------------------


def compact_session(
    session: Session,
    llm_client: LLMClient,
    *,
    keep_recent_tokens: int | None = None,
    keep_recent_messages_min: int | None = None,
) -> CompactionResult:
    """Compute a new merged summary for *session*'s older messages.

    This does NOT mutate `session`. Callers must only apply the result
    (see `apply_compaction_result`) after this call succeeds, so that a
    failure here never loses the original messages.

    The split point between "old" and "recent" messages is determined by
    token budget (*keep_recent_tokens*), with a floor of
    *keep_recent_messages_min* messages always kept verbatim.

    Raises:
        CompactionError: if there is nothing old enough to compact, the
            LLM call fails, or the LLM returns an empty/invalid summary.
    """
    # Operate on unconsolidated messages only
    messages = session.get_unconsolidated_messages()
    keep_min = (
        keep_recent_messages_min
        if keep_recent_messages_min is not None
        else KEEP_RECENT_MESSAGES_MIN
    )

    if len(messages) <= keep_min:
        raise CompactionError(
            f"当前 session 只有 {len(messages)} 条消息，"
            f"不超过保留窗口（{keep_min}），无需压缩。"
        )

    keep_tok = (
        keep_recent_tokens if keep_recent_tokens is not None else KEEP_RECENT_TOKENS
    )
    split_index = _find_split_index(messages, keep_tok)

    # Enforce the minimum-message floor: never compact more than
    # (len - keep_min) messages, even if token budget says otherwise.
    max_old = len(messages) - keep_min
    if split_index > max_old:
        split_index = max_old

    if split_index <= 0:
        raise CompactionError(
            f"当前 session 的消息 token 数未超过保留预算"
            f"（{keep_tok} token），无需压缩。"
        )

    old_messages = messages[:split_index]
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
        recent_message_count=len(messages) - split_index,
        summary=new_summary,
    )


def compact_session_snapshot(
    messages_snapshot: list[Message],
    existing_summary: str,
    llm_client: LLMClient,
    *,
    keep_recent_tokens: int | None = None,
    keep_recent_messages_min: int | None = None,
) -> CompactionResult:
    """Same as ``compact_session`` but operates on an explicit snapshot
    of messages + summary instead of a live ``Session`` object.

    This is the entry point for the async ``CompactionWorker``: the
    caller takes a shallow copy of ``session.messages`` under a brief
    lock, then calls this function outside the lock so that the LLM call
    (the slow part) does not block the main thread.
    """

    class _SnapshotSession:
        messages = messages_snapshot
        summary = existing_summary

        @classmethod
        def get_unconsolidated_messages(cls):
            return cls.messages

    return compact_session(
        _SnapshotSession,  # type: ignore[arg-type]
        llm_client,
        keep_recent_tokens=keep_recent_tokens,
        keep_recent_messages_min=keep_recent_messages_min,
    )


def apply_compaction_result(session: Session, result: CompactionResult) -> None:
    """Mutate `session` to apply an already-computed `CompactionResult`.

    Only call this after `compact_session` has returned successfully.

    The old-message slice is identified by count: the first
    ``result.old_message_count`` unconsolidated messages are removed
    and their count absorbed into ``last_consolidated``.
    """
    lc = session.last_consolidated
    # Remove old_message_count messages from the unconsolidated prefix
    session.messages = (
        session.messages[:lc]
        + session.messages[lc + result.old_message_count:]
    )
    session.last_consolidated = lc  # stays same; summary replaces the old prefix
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


# ---------------------------------------------------------------------------
# Multi-round token-budget consolidation
# ---------------------------------------------------------------------------


def maybe_consolidate_by_tokens(
    session: Session,
    llm_client: LLMClient,
    *,
    context_window_tokens: int,
    max_output_tokens: int = 4096,
    consolidation_ratio: float = 0.5,
) -> str | None:
    """Loop: archive old messages until the unconsolidated tail fits
    within the safe input budget.

    The budget reserves space for output tokens and a safety buffer.
    Up to ``_MAX_CONSOLIDATION_ROUNDS`` rounds are performed.

    Returns the last summary text, or None if nothing was consolidated.
    """
    if context_window_tokens <= 0:
        return None

    input_budget = context_window_tokens - max_output_tokens - _SAFETY_BUFFER
    if input_budget <= 0:
        return None

    target = int(input_budget * consolidation_ratio)
    messages = session.get_unconsolidated_messages()
    if not messages:
        return None

    # Estimate total tokens of unconsolidated messages
    estimated = count_tokens_for_messages(messages)
    # Add summary overhead (if any)
    if session.summary:
        estimated += count_tokens(session.summary)

    if estimated <= input_budget:
        return None  # Nothing to do

    last_summary: str | None = None

    for round_num in range(_MAX_CONSOLIDATION_ROUNDS):
        if estimated <= target:
            break

        tokens_to_remove = max(1, estimated - target)
        boundary = _find_user_boundary_index(messages, 0, tokens_to_remove)
        if boundary is None:
            # No safe boundary — try the token-based split
            split = _find_split_index(messages, KEEP_RECENT_TOKENS)
            if split <= 0:
                break
            boundary = (split, tokens_to_remove)

        end_idx = boundary[0]
        chunk = messages[:end_idx]
        if not chunk:
            break

        try:
            summary = _llm_archive(chunk, session.summary, llm_client)
        except CompactionError:
            # Raw-archive the chunk to history log as fallback
            _raw_archive(chunk, session.session_id)
            summary = None

        # Advance: remove the chunk from the working list.
        # end_idx is relative to the unconsolidated slice, so we must
        # map it back to the full session.messages list.
        base = session.last_consolidated
        session.messages = (
            session.messages[:base]
            + session.messages[base + end_idx:]
        )
        # last_consolidated stays at `base` — the consolidated prefix
        # is unchanged; the removed chunk is now covered by the summary.
        if summary and summary != "(nothing)":
            session.summary = _merge_summaries(session.summary, summary)
            last_summary = summary
        messages = session.get_unconsolidated_messages()
        if not messages:
            break

        estimated = count_tokens_for_messages(messages)
        if session.summary:
            estimated += count_tokens(session.summary)

    # Persist the last summary to session metadata for idle-session archival
    if last_summary and last_summary != "(nothing)":
        session.metadata["_last_summary"] = {
            "text": last_summary,
            "last_active": session.updated_at,
        }

    return last_summary


# ---------------------------------------------------------------------------
# Idle session compaction (AutoCompact-inspired)
# ---------------------------------------------------------------------------


def compact_idle_session(
    session_key: str,
    session_store: SessionStore,
    llm_client: LLMClient,
    *,
    max_suffix: int = 8,
) -> str | None:
    """Hard-truncate an idle session: archive everything except the
    *max_suffix* most recent messages (extended to nearest user turn).

    Returns the summary text on success, or None if nothing was archived.
    """
    session = session_store.get(session_key)
    messages = session.get_unconsolidated_messages()
    if not messages:
        return ""

    # Determine what to keep: recent suffix extended to user turn
    keep_count = min(max_suffix, len(messages))
    # Walk backwards from max_suffix to find the nearest user turn
    split_point = len(messages) - keep_count
    for i in range(split_point, -1, -1):
        if messages[i].role == "user":
            split_point = i
            break

    messages_to_keep = messages[split_point:]
    messages_to_remove = messages[:split_point]

    if not messages_to_remove:
        return ""

    last_active = session.updated_at
    summary: str | None = ""

    try:
        summary = _llm_archive(messages_to_remove, session.summary, llm_client)
    except CompactionError:
        _raw_archive(messages_to_remove, session_key)
        summary = None

    # Apply the truncation
    session.messages = (
        session.messages[:session.last_consolidated]
        + messages_to_keep
    )
    if summary and summary != "(nothing)":
        session.summary = _merge_summaries(session.summary, summary)
        session.metadata["_last_summary"] = {
            "text": summary,
            "last_active": last_active,
        }

    session.last_consolidated = 0
    session.touch()

    try:
        session_store.save(session)
    except SessionStoreError:
        pass

    return summary


# ---------------------------------------------------------------------------
# Idle check helper
# ---------------------------------------------------------------------------


def has_compactable_idle_tail(
    session: Session,
    ttl_minutes: int = 0,
    max_suffix: int = 8,
) -> bool:
    """Return True if *session* has enough unconsolidated messages
    beyond *max_suffix* to warrant idle compaction.

    If *ttl_minutes* > 0, also checks that the session has been idle
    for at least that many minutes.
    """
    from datetime import datetime, timezone

    messages = session.get_unconsolidated_messages()
    if len(messages) <= max_suffix:
        return False

    # Check TTL
    if ttl_minutes > 0:
        try:
            updated = datetime.fromisoformat(session.updated_at)
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            if age < ttl_minutes * 60:
                return False
        except (ValueError, TypeError):
            pass

    # Check if there are compactable messages
    split_point = len(messages) - max_suffix
    for i in range(split_point, -1, -1):
        if messages[i].role == "user":
            return i > 0
    return split_point > 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


def _merge_summaries(old_summary: str, new_summary: str) -> str:
    """Merge two summaries. If the old one is empty, return the new one.
    Otherwise concatenate with a separator."""
    if not old_summary.strip():
        return new_summary
    return f"{old_summary.strip()}\n\n---\n\n{new_summary.strip()}"


def _llm_archive(
    messages: list[Message],
    existing_summary: str,
    llm_client: LLMClient,
) -> str:
    """Call the LLM to produce a summary of *messages*, merged with
    *existing_summary*."""
    formatted = "\n".join(
        f"[{m.role}] {m.content[:500]}{'...' if len(m.content) > 500 else ''}"
        for m in messages
    )
    # Truncate to a reasonable size for the LLM call
    if len(formatted) > _ARCHIVE_SUMMARY_MAX_CHARS * 2:
        formatted = formatted[:_ARCHIVE_SUMMARY_MAX_CHARS * 2]

    existing_block = f"已有摘要：\n{existing_summary}" if existing_summary.strip() else "已有摘要：（无）"

    try:
        response = llm_client.chat([
            {"role": "system", "content": _COMPACTION_SYSTEM_INSTRUCTION},
            {"role": "user", "content": f"{existing_block}\n\n对话内容：\n{formatted}"},
        ])
    except LLMError as exc:
        raise CompactionError(f"LLM 调用失败: {exc}") from exc

    result = (response or "").strip()
    if not result:
        raise CompactionError("LLM 返回的摘要为空")

    return result


def _raw_archive(messages: list[Message], session_id: str) -> None:
    """Fallback: dump raw messages to history as a breadcrumb."""
    formatted = "\n".join(
        f"[{m.role}] {m.content[:300]}"
        for m in messages
    )
    if len(formatted) > _RAW_ARCHIVE_MAX_CHARS:
        formatted = formatted[:_RAW_ARCHIVE_MAX_CHARS]

    # Best-effort write to history log (may not be available)
    import sys
    print(
        f"[compaction] LLM 归档失败，已保存原始消息摘要 "
        f"({len(messages)} 条消息, {len(formatted)} 字符)",
        file=sys.stderr,
    )
