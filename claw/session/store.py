"""Persistent session storage — v5.

Key improvements over v4:

- **JSONL format**: one metadata line + one JSON per message.  Append-friendly,
  corruption-resistant (a bad line only loses one message, not the whole file).
- **Base64 key encoding**: collision-resistant filenames.
- **Atomic writes**: write to ``.tmp``, ``os.replace()``, optional ``fsync``.
- **Crash recovery**: runtime checkpoints + pending-user-turn markers.
- **Session forking**: ``fork_session_before_user_index()``.
- **File cap enforcement**: ``enforce_file_cap()`` with archive callback.
- **Legacy migration**: auto-migrate old ``session.json`` to JSONL.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claw.session.models import Message, Session, _now_iso

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILE_MAX_MESSAGES = 2000
MIN_REPLAY_MESSAGES = 120
REPLAY_TOKENS_PER_MESSAGE = 100  # rough estimate for max_messages from context_window

_SESSION_FILE_EXT = ".jsonl"
_LEGACY_FILE_NAME = "session.json"
_SESSION_PREVIEW_MAX_CHARS = 120

# Metadata keys that are volatile (should not be forked)
_FORK_VOLATILE_META = frozenset({
    "goal_state", "pending_user_turn", "runtime_checkpoint",
    "title", "title_user_edited",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def replay_max_messages_for_context(context_window_tokens: int | None) -> int:
    """Derive a sensible max replay message count from context window size."""
    if not context_window_tokens or context_window_tokens <= 0:
        return FILE_MAX_MESSAGES
    return min(
        FILE_MAX_MESSAGES,
        max(MIN_REPLAY_MESSAGES, context_window_tokens // REPLAY_TOKENS_PER_MESSAGE),
    )


def _text_preview(content: Any, max_chars: int = _SESSION_PREVIEW_MAX_CHARS) -> str:
    """Return a compact display text for session lists."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = " ".join(parts)
    else:
        return ""
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars - 1].rstrip() + "…"
    return text


def _metadata_title(metadata: dict | None) -> str:
    """Extract a display title from session metadata."""
    if not isinstance(metadata, dict):
        return ""
    title = metadata.get("title")
    return str(title) if isinstance(title, str) else ""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionStoreError(RuntimeError):
    """User-facing session storage failure."""


class SessionNotFoundError(SessionStoreError):
    """Raised when a session id does not exist."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    """Lightweight metadata used for session listing."""

    session_id: str
    title: str
    message_count: int
    updated_at: str
    preview: str = ""
    path: str = ""


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


class SessionStore:
    """JSONL-backed session manager — v5.

    Directory layout::

        data/sessions/
        ├── <base64(key)>.jsonl    # one metadata line + one JSON per message
        └── ...

    Each ``.jsonl`` file starts with a metadata line::

        {"_type": "metadata", "key": "...", "created_at": "...",
         "updated_at": "...", "metadata": {...}, "last_consolidated": 0}

    Followed by one JSON object per message.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, sessions_dir: Path):
        self._sessions_dir = Path(sessions_dir)
        self._cache: dict[str, Session] = {}
        self.load_warnings: list[str] = []
        self._legacy_sessions_dir: Path | None = None  # set for migration
        self._load_all()

    @property
    def default_session_id(self) -> str:
        return "default"

    @property
    def default_session_title(self) -> str:
        return "默认会话"

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Scan sessions directory and load all JSONL files into cache."""
        self._cache.clear()
        self.load_warnings.clear()

        if not self._sessions_dir.exists():
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            return

        for entry in sorted(self._sessions_dir.iterdir()):
            if not entry.is_file() or entry.suffix != _SESSION_FILE_EXT:
                # Check for legacy session.json in subdirectory
                if entry.is_dir():
                    legacy = entry / _LEGACY_FILE_NAME
                    if legacy.exists():
                        self._migrate_legacy(legacy, entry.name)
                continue

            try:
                session = self._load_jsonl(entry)
            except SessionStoreError as exc:
                backup = self._quarantine(entry)
                self.load_warnings.append(
                    f"session `{entry.stem}` 数据已损坏，"
                    f"已备份为 {backup.name} 并跳过加载。详情：{exc}"
                )
                continue

            self._cache[session.session_id] = session

    def _load_jsonl(self, path: Path) -> Session:
        """Load a session from a JSONL file.

        Raises SessionStoreError if the file is missing a valid metadata
        header (truly corrupt) or cannot be read.
        """
        messages: list[Message] = []
        metadata: dict = {}
        created_at: str | None = None
        updated_at: str | None = None
        last_consolidated = 0
        stored_key: str | None = None
        skipped = 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue

                    if data.get("_type") == "metadata":
                        stored_key = data.get("key")
                        metadata = data.get("metadata") or {}
                        created_at = data.get("created_at")
                        updated_at = data.get("updated_at")
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        try:
                            messages.append(Message.from_jsonl_dict(data))
                        except (ValueError, TypeError):
                            skipped += 1
                            continue
        except OSError as exc:
            raise SessionStoreError(f"无法读取 session 文件 {path}: {exc}") from exc

        # Must have a valid metadata header
        if stored_key is None:
            raise SessionStoreError(f"session 文件缺少有效的元数据头: {path}")

        session = Session(
            session_id=stored_key or self._decode_key(path.stem) or path.stem,
            title=_metadata_title(metadata) or stored_key or path.stem,
            messages=messages,
            summary=str(metadata.get("summary", "")),
            created_at=str(created_at or _now_iso()),
            updated_at=str(updated_at or _now_iso()),
            last_consolidated=int(last_consolidated),
            metadata=dict(metadata),
        )

        if skipped > 0:
            # Produce a load warning via the caller
            self.load_warnings.append(
                f"session `{session.session_id}` 在加载时跳过了 {skipped} 行损坏数据"
            )

        return session

    # ------------------------------------------------------------------
    # Legacy migration (old session.json → JSONL)
    # ------------------------------------------------------------------

    def _migrate_legacy(self, legacy_path: Path, dir_name: str) -> None:
        """Migrate an old ``session.json`` subdirectory to JSONL."""
        try:
            raw = legacy_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return

        try:
            session = Session.from_dict(data)
        except (ValueError, KeyError):
            return

        # Write as JSONL
        jsonl_path = self._key_path(session.session_id)
        if jsonl_path.exists():
            return  # already migrated

        try:
            self._write_jsonl(session, jsonl_path)
        except OSError:
            return

        # Rename legacy file
        backup = legacy_path.with_name(f"{_LEGACY_FILE_NAME}.migrated")
        try:
            legacy_path.rename(backup)
        except OSError:
            pass

        # Remove empty directory
        try:
            legacy_path.parent.rmdir()
        except OSError:
            pass

        self._cache[session.session_id] = session
        print(f"[session] 已从旧格式迁移 session: {session.session_id}")

    # ------------------------------------------------------------------
    # Key encoding (Base64 URL-safe, collision-resistant)
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_key(key: str) -> str:
        return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_key(stem: str) -> str | None:
        try:
            padding = 4 - len(stem) % 4
            if padding != 4:
                stem += "=" * padding
            return base64.urlsafe_b64decode(stem).decode("utf-8")
        except Exception:
            return None

    def _key_path(self, key: str) -> Path:
        return self._sessions_dir / f"{self._encode_key(key)}{_SESSION_FILE_EXT}"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def ensure_default_session(self) -> Session:
        """Return any existing session, or create the default one."""
        if self._cache:
            return next(iter(self._cache.values()))
        return self.create_session(
            session_id=self.default_session_id,
            title=self.default_session_title,
        )

    def create_session(
        self, title: str | None = None, session_id: str | None = None
    ) -> Session:
        resolved_id = session_id or self._generate_session_id()
        if resolved_id in self._cache:
            raise SessionStoreError(f"session id 已存在: {resolved_id}")
        session = Session(
            session_id=resolved_id,
            title=title or resolved_id,
        )
        self._cache[resolved_id] = session
        self.save(session)
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self._cache[session_id]
        except KeyError as exc:
            raise SessionNotFoundError(f"未找到 session: {session_id}") from exc

    def exists(self, session_id: str) -> bool:
        return session_id in self._cache

    def invalidate(self, session_id: str) -> None:
        """Remove from cache (force re-read from disk on next get)."""
        self._cache.pop(session_id, None)

    def rename(self, session_id: str, new_title: str) -> Session:
        session = self.get(session_id)
        session.title = new_title
        session.metadata["title"] = new_title
        session.metadata["title_user_edited"] = True
        session.touch()
        self.save(session)
        return session

    def delete(self, session_id: str) -> bool:
        """Delete a session from disk and cache. Returns True if deleted."""
        self.get(session_id)  # raises if missing
        del self._cache[session_id]
        path = self._key_path(session_id)
        deleted = False
        if path.exists():
            try:
                path.unlink()
                deleted = True
            except OSError:
                pass
        return deleted

    # ------------------------------------------------------------------
    # Persistence (JSONL, atomic write)
    # ------------------------------------------------------------------

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """Save session to disk atomically.

        When *fsync* is True, the file and its parent directory are
        explicitly flushed (for graceful shutdown).
        """
        path = self._key_path(session.session_id)
        tmp_path = path.with_suffix(f"{_SESSION_FILE_EXT}.tmp")

        try:
            self._write_jsonl(session, tmp_path, fsync=fsync)
            os.replace(tmp_path, path)

            if fsync:
                with suppress(PermissionError):
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise SessionStoreError(
                f"保存 session {session.session_id} 失败: {exc}"
            ) from exc

        self._cache[session.session_id] = session

    @staticmethod
    def _write_jsonl(session: Session, path: Path, *, fsync: bool = False) -> None:
        """Write a session to a JSONL file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            # Metadata line
            meta = {
                "_type": "metadata",
                "key": session.session_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
            }
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            # Message lines
            for msg in session.messages:
                f.write(json.dumps(msg.to_jsonl_dict(), ensure_ascii=False) + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())

    def flush_all(self) -> int:
        """Re-save every cached session with fsync. Returns count flushed."""
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                pass
        return flushed

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_summaries(self) -> list[SessionSummary]:
        summaries = []
        for session in self._cache.values():
            preview = ""
            for msg in session.messages:
                if msg.role == "user" and not msg._command:
                    preview = _text_preview(msg.content)
                    break
                if not preview and msg.role == "assistant":
                    preview = _text_preview(msg.content)
            summaries.append(SessionSummary(
                session_id=session.session_id,
                title=_metadata_title(session.metadata) or session.title,
                message_count=len(session.messages),
                updated_at=session.updated_at,
                preview=preview,
                path=str(self._key_path(session.session_id)),
            ))
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    def set_runtime_checkpoint(self, session: Session, payload: dict) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.save(session)

    def mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def clear_runtime_checkpoint(self, session: Session) -> None:
        session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    def clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an interrupted turn into session history.

        Returns True if a checkpoint was found and restored.
        """
        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_msg = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored: list[Message] = []
        if isinstance(assistant_msg, dict):
            restored.append(Message.from_jsonl_dict(assistant_msg))
        for tr in completed_tool_results:
            if isinstance(tr, dict):
                restored.append(Message.from_jsonl_dict(tr))
        for tc in pending_tool_calls:
            if not isinstance(tc, dict):
                continue
            tool_id = tc.get("id", "")
            fn = tc.get("function") or {}
            name = fn.get("name", "tool")
            restored.append(Message(
                role="tool",
                content="Error: Task interrupted before this tool finished.",
                tool_call_id=tool_id,
                name=name,
            ))

        # Dedup: find overlap with existing session tail
        overlap = 0
        max_overlap = min(len(session.messages), len(restored))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored_slice = restored[:size]
            if all(
                _msg_key(left) == _msg_key(right)
                for left, right in zip(existing, restored_slice)
            ):
                overlap = size
                break

        session.messages.extend(restored[overlap:])
        self.clear_pending_user_turn(session)
        self.clear_runtime_checkpoint(session)
        return True

    def restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that was interrupted after the user message was
        persisted but before a response was generated."""
        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].role == "user":
            session.append_message(
                "assistant",
                "Error: Task interrupted before a response was generated.",
            )

        self.clear_pending_user_turn(session)
        return True

    # ------------------------------------------------------------------
    # Session forking
    # ------------------------------------------------------------------

    def fork_session_before_user_index(
        self,
        source_key: str,
        target_key: str,
        before_user_index: int,
    ) -> Session | None:
        """Create *target_key* from *source_key*, truncated before the
        N-th user message (0-indexed over user messages).

        ``before_user_index=0`` means "before the first user message",
        ``1`` means "before the second", etc.  A value equal to the total
        user count copies the full prefix.
        """
        if before_user_index < 0:
            return None

        source = self._cache.get(source_key)
        if source is None:
            return None

        copied: list[Message] = []
        user_count = 0
        found = False
        for msg in source.messages:
            if msg.role == "user":
                if user_count == before_user_index:
                    found = True
                    break
                user_count += 1
            copied.append(msg)

        if user_count == before_user_index:
            found = True
        if not found:
            return None

        # Deep-copy metadata, stripping volatile keys
        import copy
        metadata = copy.deepcopy(source.metadata)
        for key in _FORK_VOLATILE_META:
            metadata.pop(key, None)

        last_consolidated = min(source.last_consolidated, len(copied))
        if source.last_consolidated > len(copied):
            metadata.pop("_last_summary", None)
            last_consolidated = 0

        target = Session(
            session_id=target_key,
            title=f"{source.title} (fork)",
            messages=[copy.deepcopy(m) for m in copied],
            summary=source.summary if last_consolidated > 0 else "",
            created_at=_now_iso(),
            updated_at=_now_iso(),
            last_consolidated=last_consolidated,
            metadata=metadata,
        )
        self.save(target, fsync=True)
        return target

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _generate_session_id(self) -> str:
        pat = re.compile(r"^session_(\d+)$")
        max_seq = 0
        for eid in self._cache:
            m = pat.match(eid)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return f"session_{max_seq + 1:03d}"

    @staticmethod
    def _quarantine(path: Path) -> Path:
        """Rename a corrupted file instead of deleting it."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.corrupted-{ts}")
        try:
            path.rename(backup)
        except OSError:
            return path
        return backup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg_key(msg: Message) -> tuple:
    """Return a stable identity key for dedup during checkpoint restore."""
    return (
        msg.role,
        msg.content,
        msg.tool_call_id,
        msg.name,
        tuple(
            (tc.get("id"), tc.get("function", {}).get("name"))
            for tc in (msg.tool_calls or [])
            if isinstance(tc, dict)
        ) if msg.tool_calls else None,
    )
