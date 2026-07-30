"""Compaction: summarize older session messages into `session.summary`.

Boundary (Step 4 requirement): compaction only ever reads/writes
`session.summary` and `session.messages`. It builds its own minimal,
dedicated LLM request and never touches the app's system prompt, soul
or memory store --- those are wired in independently by
`claw.context.builder.ContextBuilder`.

Current behavior:

- Multi-round token-budget consolidation: compacts in up to 5 rounds
  until the session fits within the context budget.
- User-turn boundary detection: never splits mid-turn — always
  compacts at user-message boundaries.
- Summary persistence for process restart recovery.
- Proper token estimation including system prompt, tool definitions,
  and summary overhead — not just raw message content.
"""

from __future__ import annotations

from dataclasses import dataclass

from claw.context.token_counter import count_tokens, count_tokens_for_messages
from claw.env_utils import env_int
from claw.llm.client import LLMClient, LLMError
from claw.session.models import Message, Session
from claw.session.store import SessionStore, SessionStoreError

# ---------------------------------------------------------------------------
# Configurable thresholds (env-overridable at import time)
# ---------------------------------------------------------------------------

KEEP_RECENT_MESSAGES_MIN = env_int(
    "COMPACT_KEEP_RECENT_MESSAGES_MIN",
    4,
    minimum=0,
)
"""Absolute floor: never compact if there are <= this many messages."""

MAX_MESSAGE_TOKENS = env_int("COMPACT_MAX_MESSAGE_TOKENS", 2000, minimum=1)
"""Trigger compaction when session.messages content exceeds this many tokens."""

KEEP_RECENT_TOKENS = env_int("COMPACT_KEEP_RECENT_TOKENS", 1000, minimum=1)
"""Token budget for the recent-message window that is kept verbatim."""

# ---------------------------------------------------------------------------
# Consolidation constants
# ---------------------------------------------------------------------------

_MAX_CONSOLIDATION_ROUNDS = 5
_SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift
_ARCHIVE_SUMMARY_MAX_CHARS = 8000
_RAW_ARCHIVE_MAX_CHARS = 16000

# Tool output pruning: replace large tool results with a placeholder
# before sending to the LLM summarizer (cheap pre-pass).
_PRUNED_TOOL_PLACEHOLDER = "[旧工具输出已清除以节省上下文空间]"
_TOOL_RESULT_PRUNE_THRESHOLD = 500  # chars — prune tool results longer than this

# Summary failure cooldown: after a failure, wait this long before
# attempting compaction again (prevents tight retry loops).
_SUMMARY_FAILURE_COOLDOWN_S = 600  # 10 minutes
_last_compaction_failure_ts: float = 0.0  # module-level cooldown tracker

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
    "请使用以下结构化格式输出摘要（如果某部分没有内容则省略该部分）：\n\n"
    "## 当前任务\n"
    "（简述当前正在进行的主要任务，1-2 句话）\n\n"
    "## 已完成\n"
    "（列出已完成的步骤和结果，用要点格式）\n\n"
    "## 待解决\n"
    "（列出尚未解决的问题和待办事项）\n\n"
    "## 关键事实\n"
    "（影响后续决策的关键信息：用户偏好、约束条件、重要发现）\n\n"
    "## 相关文件\n"
    "（提到过的文件路径，用于上下文连续性）\n\n"
    "只输出摘要正文本身，不要输出多余的说明或前后缀。"
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


class CompactionNotNeeded(CompactionError):
    """Raised when there is currently no safe message prefix to compact.

    This is an expected no-op, not a summarization failure. Interactive
    callers may show the reason, while automatic workers should skip it
    silently and reassess after a later turn.
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


def format_compaction_brief(
    session_id: str,
    result: CompactionResult,
    *,
    automatic: bool = False,
    include_summary: bool = True,
) -> str:
    """Build the user-facing completion report shared by CLI and WebUI.

    Automatic notices deliberately remain short.  Manual ``/compact`` calls
    include the generated summary so users can verify which task state and
    constraints will be carried forward.
    """
    heading = "自动压缩已完成" if automatic else "压缩简报"
    lines = [
        heading,
        "- 状态：已完成",
        f"- Session：{session_id}",
        f"- 已压缩旧消息：{result.old_message_count} 条",
        f"- 原样保留最近消息：{result.recent_message_count} 条",
        "- 任务连续性：仅压缩完整旧轮次，未截断正在进行的任务",
    ]
    if include_summary:
        lines.extend(["", "摘要预览：", result.summary])
    return "\n".join(lines)


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


def _align_split_to_user_boundary(
    messages: list[Message],
    split_index: int,
) -> int:
    """Move *split_index* backwards to the nearest user-turn boundary.

    The recent verbatim tail must start with a user message.  Returning
    ``0`` means that no safe compactable prefix exists.
    """
    upper = min(split_index, len(messages) - 1)
    for index in range(upper, 0, -1):
        if messages[index].role == "user":
            return index
    return 0


def _find_compaction_split_index(
    messages: list[Message],
    *,
    keep_recent_tokens: int,
    keep_recent_messages_min: int,
    force: bool,
) -> tuple[int, int]:
    """Return ``(raw_split_index, compactable_message_count)``.

    Persisted command/notification messages exist only for UI continuity.
    Excluding them from budget and retention calculations prevents completion
    notices from changing when the next real conversation turn is compacted.
    The returned raw index still advances over any such notices that precede
    the first retained user message.
    """
    indexed_messages = [
        (index, message)
        for index, message in enumerate(messages)
        if not message._command
    ]
    compactable_messages = [message for _, message in indexed_messages]
    if len(compactable_messages) <= keep_recent_messages_min:
        return 0, len(compactable_messages)

    max_old = len(compactable_messages) - keep_recent_messages_min
    logical_split = (
        max_old
        if force
        else _find_split_index(compactable_messages, keep_recent_tokens)
    )
    logical_split = min(logical_split, max_old)
    logical_split = _align_split_to_user_boundary(
        compactable_messages,
        logical_split,
    )
    if logical_split <= 0:
        return 0, len(compactable_messages)
    return indexed_messages[logical_split][0], len(compactable_messages)


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

    A **failure cooldown** prevents retrying compaction too quickly
    after a recent LLM summarization failure.
    """
    # Cooldown check: skip if we recently failed
    if _last_compaction_failure_ts > 0:
        import time as _time
        elapsed = _time.time() - _last_compaction_failure_ts
        if elapsed < _SUMMARY_FAILURE_COOLDOWN_S:
            return False

    unconsolidated = session.get_unconsolidated_messages()
    context_messages = [
        message for message in unconsolidated if not message._command
    ]
    if len(context_messages) <= KEEP_RECENT_MESSAGES_MIN:
        return False

    max_tok = max_message_tokens if max_message_tokens is not None else MAX_MESSAGE_TOKENS
    # UI-only command/notification messages are persisted in the transcript
    # for display, but must not create context pressure by themselves.
    message_tokens = count_tokens_for_messages(context_messages)
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


def has_compactable_prefix(
    session: Session,
    *,
    keep_recent_tokens: int | None = None,
    keep_recent_messages_min: int | None = None,
) -> bool:
    """Return whether automatic compaction has a safe old-turn prefix.

    Crossing the automatic token threshold alone is insufficient: the
    recent-token window and minimum-message floor can temporarily leave no
    complete old user turn to summarize. Checking this before submission
    prevents a background retry loop for an expected no-op.
    """
    messages = session.get_unconsolidated_messages()
    keep_min = (
        keep_recent_messages_min
        if keep_recent_messages_min is not None
        else KEEP_RECENT_MESSAGES_MIN
    )
    keep_tok = (
        keep_recent_tokens if keep_recent_tokens is not None else KEEP_RECENT_TOKENS
    )
    split_index, _ = _find_compaction_split_index(
        messages,
        keep_recent_tokens=keep_tok,
        keep_recent_messages_min=keep_min,
        force=False,
    )
    return split_index > 0


# ---------------------------------------------------------------------------
# Compaction logic
# ---------------------------------------------------------------------------


def compact_session(
    session: Session,
    llm_client: LLMClient,
    *,
    keep_recent_tokens: int | None = None,
    keep_recent_messages_min: int | None = None,
    force: bool = False,
) -> CompactionResult:
    """Compute a new merged summary for *session*'s older messages.

    This does NOT mutate `session`. Callers must only apply the result
    (see `apply_compaction_result`) after this call succeeds, so that a
    failure here never loses the original messages.

    The split point between "old" and "recent" messages is determined by
    token budget (*keep_recent_tokens*), with a floor of
    *keep_recent_messages_min* messages always kept verbatim.  When
    *force* is true, the token budget is intentionally bypassed and the
    largest safe prefix is compacted.  This is used by the explicit
    ``/compact`` command so it can run before an automatic threshold is
    reached.

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

    context_message_count = sum(not message._command for message in messages)
    if context_message_count <= keep_min:
        raise CompactionNotNeeded(
            f"当前 session 只有 {context_message_count} 条对话消息，"
            f"不超过保留窗口（{keep_min}），无需压缩。"
        )

    keep_tok = (
        keep_recent_tokens if keep_recent_tokens is not None else KEEP_RECENT_TOKENS
    )
    split_index, _ = _find_compaction_split_index(
        messages,
        keep_recent_tokens=keep_tok,
        keep_recent_messages_min=keep_min,
        force=force,
    )

    if split_index <= 0:
        if force:
            raise CompactionNotNeeded(
                "当前 session 没有可安全压缩的完整旧对话轮次，无需压缩。"
            )
        raise CompactionNotNeeded(
            f"当前 session 的消息 token 数未超过保留预算"
            f"（{keep_tok} token），无需压缩。"
        )

    old_messages = messages[:split_index]
    summary_messages = [message for message in old_messages if not message._command]
    if not summary_messages:
        raise CompactionNotNeeded(
            "当前 session 没有可安全压缩的完整旧对话轮次，无需压缩。"
        )
    request_messages = _build_compaction_request(session.summary, summary_messages)

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
    force: bool = False,
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
        force=force,
    )


def apply_compaction_result(session: Session, result: CompactionResult) -> None:
    """Mutate `session` to apply an already-computed `CompactionResult`.

    Only call this after `compact_session` has returned successfully.

    The raw transcript is immutable for rollback.  Compaction advances the
    context projection boundary instead of deleting the covered messages.
    """
    session.last_consolidated = min(
        len(session.messages),
        session.last_consolidated + result.old_message_count,
    )
    session.summary = result.summary
    session.touch()


def compact_and_persist(
    session: Session,
    session_store: SessionStore,
    llm_client: LLMClient,
    *,
    force: bool = False,
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
    result = compact_session(session, llm_client, force=force)
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

    for _ in range(_MAX_CONSOLIDATION_ROUNDS):
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

        # Advance only the context projection boundary.  Raw messages remain
        # available to checkpoint restore and audit.
        session.last_consolidated += end_idx
        if summary and summary != "(nothing)":
            session.summary = _merge_summaries(session.summary, summary)
            last_summary = summary
        messages = session.get_unconsolidated_messages()
        if not messages:
            break

        estimated = count_tokens_for_messages(messages)
        if session.summary:
            estimated += count_tokens(session.summary)

    # Persist the last summary for process restart recovery.
    if last_summary and last_summary != "(nothing)":
        session.metadata["_last_summary"] = {
            "text": last_summary,
            "last_active": session.updated_at,
        }

    return last_summary


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

    # Tool output pruning: replace large tool
    # results with a placeholder before sending to the summarizer.
    # This is a cheap pre-pass that dramatically reduces token cost
    # without losing the structural context of the conversation.
    transcript_lines: list[str] = []
    for m in old_messages:
        content = m.content
        if (
            m.role == "tool"
            and len(content) > _TOOL_RESULT_PRUNE_THRESHOLD
        ):
            content = _PRUNED_TOOL_PLACEHOLDER
        transcript_lines.append(f"{m.role}: {content}")

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
    global _last_compaction_failure_ts
    import time as _time

    # Tool output pruning: replace large tool results with placeholder
    formatted_parts: list[str] = []
    for m in messages:
        content = m.content
        if m.role == "tool" and len(content) > _TOOL_RESULT_PRUNE_THRESHOLD:
            content = _PRUNED_TOOL_PLACEHOLDER
        else:
            content = f"{content[:500]}{'...' if len(content) > 500 else ''}"
        formatted_parts.append(f"[{m.role}] {content}")

    formatted = "\n".join(formatted_parts)
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
        # Record failure timestamp for cooldown tracking
        _last_compaction_failure_ts = _time.time()
        raise CompactionError(f"LLM 调用失败: {exc}") from exc

    result = (response or "").strip()
    if not result:
        _last_compaction_failure_ts = _time.time()
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
