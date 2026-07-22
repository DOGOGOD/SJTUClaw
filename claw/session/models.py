"""Session and message data models — v5.

Key improvements over v3/v4:

- ``last_consolidated`` tracks how many messages have been summarized.
- ``metadata`` carries ``_last_summary``, runtime checkpoints, goal state.
- Messages support ``tool_calls``, ``tool_call_id``, ``name`` for native
  function-calling persistence.
- ``_command`` flag marks slash-command messages for filtering from replay.
- ``to_jsonl_dict()`` / ``from_jsonl_dict()`` for JSONL serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


from claw.utils import now_iso as _now_iso


# Keys that should NOT be persisted to disk (ephemeral runtime state).
_VOLATILE_MESSAGE_KEYS = frozenset({
    "_volatile",  # generic marker
})

# Metadata keys that should be stripped when forking a session.
_FORK_VOLATILE_METADATA_KEYS = frozenset({
    "goal_state",
    "pending_user_turn",
    "runtime_checkpoint",
    "title",
    "title_user_edited",
})


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single chat message with optional tool-call / tool-result fields.

    The ``content`` field is always a string for storage simplicity.
    Multimodal blocks (images, etc.) are stored as placeholder text
    (e.g. ``[image: path]``) and reconstructed during replay.
    """

    role: str          # "user" | "assistant" | "tool" | "system"
    content: str

    # Stable identifiers are required by workspace checkpoints.  They are
    # deliberately independent from list indexes because compaction and tool
    # messages can change the materialized message layout.
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    rollback_checkpoint_id: str | None = None

    # -- Native function-calling fields (optional) --
    tool_calls: list[dict] | None = None     # assistant: tool call requests
    tool_call_id: str | None = None           # tool: result identifier
    name: str | None = None                   # tool: tool name

    # -- Metadata (optional, non-LLM-visible) --
    timestamp: str = field(default_factory=_now_iso)
    _command: bool = False                    # slash-command, filtered from replay
    media: list[str] | None = None            # image paths for user messages
    injected_event: str | None = None         # e.g. "subagent_result"
    subagent_task_id: str | None = None
    latency_ms: int | None = None             # turn latency for the last assistant msg

    def to_dict(self) -> dict:
        """Full serialization dict (used for API responses, not disk storage)."""
        d: dict = {"role": self.role, "content": self.content}
        if self.rollback_checkpoint_id:
            d["messageId"] = self.message_id
            d["rollbackCheckpointId"] = self.rollback_checkpoint_id
            d["rollbackAvailable"] = True
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        if self._command:
            d["command"] = True
        if self.media:
            d["media"] = self.media
        return d

    def to_jsonl_dict(self) -> dict:
        """Serialize to a JSONL-line dict (for disk persistence)."""
        d: dict = {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "message_id": self.message_id,
        }
        if self.rollback_checkpoint_id:
            d["rollback_checkpoint_id"] = self.rollback_checkpoint_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        if self._command:
            d["_command"] = True
        if self.media:
            d["media"] = self.media
        if self.injected_event:
            d["injected_event"] = self.injected_event
        if self.subagent_task_id:
            d["subagent_task_id"] = self.subagent_task_id
        if self.latency_ms is not None:
            d["latency_ms"] = self.latency_ms
        return d

    @classmethod
    def from_jsonl_dict(cls, data: dict) -> "Message":
        role = str(data.get("role", ""))
        content = str(data.get("content", ""))
        return cls(
            role=role,
            content=content,
            message_id=str(data.get("message_id") or f"msg_{uuid.uuid4().hex}"),
            rollback_checkpoint_id=(
                str(data["rollback_checkpoint_id"])
                if data.get("rollback_checkpoint_id") else None
            ),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            timestamp=str(data.get("timestamp", _now_iso())),
            _command=bool(data.get("_command", data.get("command", False))),
            media=data.get("media"),
            injected_event=data.get("injected_event"),
            subagent_task_id=data.get("subagent_task_id"),
            latency_ms=data.get("latency_ms"),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Deserialize from the old single-JSON format (backward compat)."""
        try:
            role = data["role"]
            content = data["content"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"消息数据格式错误，缺少字段: {data!r}") from exc
        if not isinstance(role, str) or not isinstance(content, str):
            raise ValueError(f"消息数据格式错误，字段类型不正确: {data!r}")
        return cls(
            role=role,
            content=content,
            message_id=str(
                data.get("messageId") or data.get("message_id")
                or f"msg_{uuid.uuid4().hex}"
            ),
            rollback_checkpoint_id=(
                str(data.get("rollbackCheckpointId") or data.get("rollback_checkpoint_id"))
                if data.get("rollbackCheckpointId") or data.get("rollback_checkpoint_id")
                else None
            ),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            timestamp=str(data.get("timestamp", _now_iso())),
            _command=bool(data.get("_command", data.get("command", False))),
            media=data.get("media"),
            injected_event=data.get("injected_event"),
            subagent_task_id=data.get("subagent_task_id"),
            latency_ms=data.get("latency_ms"),
        )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """A single conversation session — v5.

    Key fields:
    - ``last_consolidated``: messages before this index are archived to summary.
    - ``metadata``: ``_last_summary``, runtime checkpoints, goal state, title.
    - ``summary``: compaction summary (incremental — merged across rounds).
    """

    session_id: str
    title: str
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    skill_usage: list = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_consolidated: int = 0
    revision: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.last_consolidated, bool)
            or not isinstance(self.last_consolidated, int)
            or not 0 <= self.last_consolidated <= len(self.messages)
        ):
            self.last_consolidated = 0

    # -- Mutation ---------------------------------------------------------

    def append_message(self, role: str, content: str, **kwargs) -> Message:
        """Add a message with optional extra fields."""
        msg = Message(role=role, content=content, **kwargs)
        self.messages.append(msg)
        self.touch()
        return msg

    def touch(self) -> None:
        self.updated_at = _now_iso()
        self.revision += 1

    def clear(self) -> None:
        """Reset session to initial state."""
        self.messages.clear()
        self.last_consolidated = 0
        self.summary = ""
        self.metadata.pop("_last_summary", None)
        try:
            generation = int(self.metadata.get("pi_session_generation", 1)) + 1
        except (TypeError, ValueError):
            generation = 2
        self.metadata["pi_session_generation"] = str(generation)
        self.touch()

    # -- History replay

    def get_unconsolidated_messages(self) -> list[Message]:
        """Return messages not yet consolidated into summary."""
        return self.messages[self.last_consolidated:]

    def get_history(
        self,
        max_messages: int = 0,
        *,
        max_tokens: int = 0,
        extend_to_user: bool = False,
    ) -> list[dict]:
        """Return unconsolidated messages suitable for LLM replay.

        History is sliced by message count first (``max_messages``), then
        by token budget from the tail (``max_tokens``) when provided.

        Messages are sanitized: internal artifacts are stripped, image
        breadcrumbs synthesized, command messages filtered out.
        """
        from claw.context.token_counter import count_tokens

        FILE_MAX_MESSAGES = 2000
        unconsolidated = self.get_unconsolidated_messages()
        max_messages = max_messages if max_messages > 0 else FILE_MAX_MESSAGES

        # Slice by message count
        if len(unconsolidated) > max_messages:
            start_idx = max(0, len(unconsolidated) - max_messages)
            if extend_to_user:
                # Walk back from start_idx to find nearest user turn
                for i in range(start_idx, -1, -1):
                    if unconsolidated[i].role == "user":
                        start_idx = i
                        break
            unconsolidated = unconsolidated[start_idx:]

        # Drop orphan tool results at the front
        unconsolidated = self._drop_front_orphans(unconsolidated)

        # Build sanitized output
        out: list[dict] = []
        for msg in unconsolidated:
            if msg._command:
                continue

            content = msg.content
            role = msg.role

            # Sanitize assistant replay text
            if role == "assistant" and isinstance(content, str):
                content = self._sanitize_assistant_replay_text(content)

            # Synthesize [image: path] breadcrumbs from persisted media
            if role == "user" and msg.media:
                breadcrumbs = "\n".join(
                    f"[image: {p}]" for p in msg.media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs

            entry: dict = {"role": msg.role, "content": content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.name:
                entry["name"] = msg.name

            out.append(entry)

        # Token-budget truncation from the tail
        if max_tokens > 0 and out:
            kept: list[dict] = []
            used = 0
            for entry in reversed(out):
                tokens = count_tokens(entry.get("content", ""))
                if kept and used + tokens > max_tokens:
                    break
                kept.append(entry)
                used += tokens
            kept.reverse()

            # Align to first user turn
            first_user = next(
                (i for i, m in enumerate(kept) if m.get("role") == "user"), None
            )
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # Fallback: find nearest user in the unsliced output
                for i in range(len(out) - 1, -1, -1):
                    if out[i].get("role") == "user":
                        kept = out[i:]
                        break

            # Check for legal start
            kept = self._drop_front_orphans_dict(kept)
            out = kept

        return out

    # -- Retention (safe truncation) --------------------------------------

    def retain_recent_legal_suffix(
        self,
        max_messages: int,
        *,
        extend_to_user: bool = False,
    ) -> tuple[list[Message], int]:
        """Keep a legal recent suffix, optionally extended to a user turn.

        Returns ``(dropped_messages, already_consolidated_count)``.
        This method **mutates** ``self.messages`` in place.
        """
        if max_messages <= 0:
            dropped = list(self.messages)
            lc = self.last_consolidated
            self.clear()
            return dropped, min(lc, len(dropped))

        if len(self.messages) <= max_messages:
            return [], 0

        original = list(self.messages)
        before_lc = self.last_consolidated

        start_idx = max(0, len(self.messages) - max_messages)
        if extend_to_user:
            for i in range(start_idx, -1, -1):
                if self.messages[i].role == "user":
                    start_idx = i
                    break

        retained = self.messages[start_idx:]

        # Prefer starting at a user turn
        first_user = next(
            (i for i, m in enumerate(retained) if m.role == "user"), None
        )
        if first_user is not None:
            retained = retained[first_user:]
        elif not extend_to_user:
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].role == "user"),
                None,
            )
            if latest_user is not None:
                retained = self.messages[latest_user: latest_user + max_messages]

        # Drop orphan tool results at the front
        retained = self._drop_front_orphans(retained)

        # Hard cap
        if not extend_to_user and len(retained) > max_messages:
            retained = retained[-max_messages:]
            retained = self._drop_front_orphans(retained)

        # Compute dropped messages
        retained_ids = set(id(m) for m in retained)
        dropped = [m for m in original if id(m) not in retained_ids]

        already_consolidated = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) not in retained_ids
        )

        # Recompute last_consolidated
        new_lc = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) in retained_ids
        )

        self.messages = retained
        self.last_consolidated = new_lc
        self.touch()
        return dropped, already_consolidated

    def enforce_file_cap(
        self,
        limit: int = 2000,
        *,
        on_archive=None,
    ) -> None:
        """Bound session message growth by archiving old prefixes."""
        if limit <= 0 or len(self.messages) <= limit:
            return

        dropped, already_lc = self.retain_recent_legal_suffix(limit)
        if not dropped:
            return

        archive_chunk = dropped[already_lc:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)

    # -- Serialization ----------------------------------------------------

    def to_dict(self) -> dict:
        """Full dict for API responses."""
        return {
            "sessionId": self.session_id,
            "title": self.title,
            "messages": [m.to_dict() for m in self.messages],
            "summary": self.summary,
            "skillUsage": [
                r if isinstance(r, dict) else r.to_dict()
                for r in self.skill_usage
            ],
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastConsolidated": self.last_consolidated,
            "revision": self.revision,
            "metadata": self.metadata,
        }

    def to_snapshot_dict(self) -> dict:
        """Lossless internal snapshot, independent from the public API shape."""
        snapshot = self.to_dict()
        snapshot["messages"] = [message.to_jsonl_dict() for message in self.messages]
        return snapshot

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Deserialize from old single-JSON format (backward compat)."""
        if not isinstance(data, dict):
            raise ValueError("Session 数据格式错误：顶层结构应为对象")
        try:
            session_id = data["sessionId"]
            title = data["title"]
        except KeyError as exc:
            raise ValueError(f"Session 数据格式错误，缺少字段: {exc}") from exc

        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ValueError("messages 字段应为数组")

        summary = str(data.get("summary") or "")
        created_at = str(data.get("createdAt") or _now_iso())
        updated_at = str(data.get("updatedAt") or created_at)
        skill_usage_raw = data.get("skillUsage") or []
        last_consolidated = int(data.get("lastConsolidated", 0))
        revision = int(data.get("revision", 0))
        metadata = dict(data.get("metadata") or {})

        messages = [Message.from_dict(item) for item in raw_messages]
        return cls(
            session_id=session_id,
            title=title,
            messages=messages,
            summary=summary,
            skill_usage=list(skill_usage_raw),
            created_at=created_at,
            updated_at=updated_at,
            last_consolidated=last_consolidated,
            revision=revision,
            metadata=metadata,
        )

    # -- Internal helpers -------------------------------------------------

    @staticmethod
    def _sanitize_assistant_replay_text(content: str) -> str:
        """Remove internal replay artifacts from assistant text.

        These strings are useful as runtime/session metadata but become
        harmful demonstrations when replayed to the model.
        """
        import re
        # Strip runtime context blocks
        content = re.sub(
            r"\[运行时上下文[^\]]*\].*?\[/运行时上下文\]",
            "",
            content,
            flags=re.DOTALL,
        )
        # Strip [Message Time: ...] prefix
        content = re.sub(r"^\[Message Time: [^\]]+\]\n?", "", content)
        # Strip local image breadcrumbs
        content = re.sub(r"^\[image: (?:/|~)[^\]]+\]\s*$", "", content, flags=re.MULTILINE)
        return content.strip()

    @staticmethod
    def _drop_front_orphans(messages: list[Message]) -> list[Message]:
        """Drop orphan tool results from the front of a message list."""
        if not messages:
            return messages
        declared: set[str] = set()
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
        first_legal = 0
        for i, m in enumerate(messages):
            if m.role == "tool":
                tid = m.tool_call_id
                if tid and str(tid) not in declared:
                    first_legal = i + 1
                    continue
            break
        return messages[first_legal:]

    @staticmethod
    def _drop_front_orphans_dict(messages: list[dict]) -> list[dict]:
        """Same as _drop_front_orphans but for dict messages."""
        if not messages:
            return messages
        declared: set[str] = set()
        for m in messages:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
        first_legal = 0
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                tid = m.get("tool_call_id")
                if tid and str(tid) not in declared:
                    first_legal = i + 1
                    continue
            break
        return messages[first_legal:]
