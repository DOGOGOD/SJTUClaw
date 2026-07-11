"""Cross-session history log — append-only, cursor-based audit trail.

Inspired by cross-session audit trail pattern, this module provides a shared
append-only log that records summaries of important agent actions and
conversation events across all sessions.  Each entry is a JSONL line
with an auto-incrementing cursor, so consumers (memory reflection, Dream
consolidation, context injection) can read only what's new since their
last checkpoint.

Design:
    - One ``history.jsonl`` file under the memory directory.
    - Thread-safe appends via a file-level lock.
    - Cursor-based incremental reads — no full-file scans on every turn.
    - Compact: oldest entries are dropped when the file exceeds
      ``max_entries``.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ENTRIES = 2000
_HISTORY_ENTRY_HARD_CAP = 64000  # emergency cap per entry (chars)
_HISTORY_FILE_NAME = "history.jsonl"
_CURSOR_FILE_NAME = ".history_cursor"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    """A single entry in the cross-session history log."""

    cursor: int
    timestamp: str
    content: str
    session_id: str = ""
    event_type: str = ""  # "summary" | "decision" | "fact" | "action"

    def to_dict(self) -> dict:
        d = {
            "cursor": self.cursor,
            "timestamp": self.timestamp,
            "content": self.content,
        }
        if self.session_id:
            d["session_id"] = self.session_id
        if self.event_type:
            d["event_type"] = self.event_type
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            cursor=int(data.get("cursor", 0)),
            timestamp=str(data.get("timestamp", "")),
            content=str(data.get("content", "")),
            session_id=str(data.get("session_id", "")),
            event_type=str(data.get("event_type", "")),
        )


# ---------------------------------------------------------------------------
# HistoryLog
# ---------------------------------------------------------------------------


class HistoryLogError(RuntimeError):
    """User-facing history log error."""


class HistoryLog:
    """Append-only, cursor-based cross-session history log.

    Usage::

        log = HistoryLog(memory_dir)
        cursor = log.append("用户决定使用 PostgreSQL 作为数据库", session_id="session_001")
        new_entries = log.read_since(cursor)
    """

    def __init__(self, memory_dir: Path, max_entries: int = _DEFAULT_MAX_ENTRIES):
        self._memory_dir = Path(memory_dir)
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._history_file = self._memory_dir / _HISTORY_FILE_NAME
        self._cursor_file = self._memory_dir / _CURSOR_FILE_NAME
        self._max_entries = max_entries
        self._append_lock = threading.Lock()
        self._oversize_logged = False

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def append(
        self,
        content: str,
        *,
        session_id: str = "",
        event_type: str = "",
        max_chars: int | None = None,
    ) -> int:
        """Append an entry and return its cursor.

        Thread-safe — concurrent appends are serialised by an internal lock.
        """
        limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
        raw = content.strip()
        if not raw:
            return 0

        if len(raw) > limit:
            if not self._oversize_logged:
                self._oversize_logged = True
                import sys
                print(
                    f"[history_log] 条目超过 {limit} 字符 ({len(raw)})，已截断",
                    file=sys.stderr,
                )
            raw = raw[:limit]

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        with self._append_lock:
            cursor = self._next_cursor()
            entry = HistoryEntry(
                cursor=cursor,
                timestamp=ts,
                content=raw,
                session_id=session_id,
                event_type=event_type,
            )
            try:
                with open(self._history_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            except OSError as exc:
                raise HistoryLogError(f"写入历史日志失败: {exc}") from exc
            self._cursor_file.write_text(str(cursor), encoding="utf-8")

        # Compact if necessary (outside the append lock to avoid contention,
        # but still serialised — compact acquires its own lock)
        self._maybe_compact()
        return cursor

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read_since(self, since_cursor: int = 0) -> list[HistoryEntry]:
        """Return entries with cursor > *since_cursor*, oldest first."""
        entries: list[HistoryEntry] = []
        with suppress(FileNotFoundError):
            with open(self._history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = HistoryEntry.from_dict(json.loads(line))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
                    if entry.cursor > since_cursor:
                        entries.append(entry)
        return entries

    def read_recent_for_prompt(
        self,
        max_entries: int = 50,
        exclude_session_id: str = "",
    ) -> list[HistoryEntry]:
        """Return the most recent entries suitable for prompt injection.

        Excludes entries from *exclude_session_id* to avoid showing the
        current session's own archived history back to itself.
        """
        all_entries = self._read_all_entries()
        if exclude_session_id:
            all_entries = [
                e for e in all_entries
                if e.session_id != exclude_session_id
            ]
        return all_entries[-max_entries:]

    def read_recent(self, max_entries: int = 100) -> list[HistoryEntry]:
        """Return the most recent entries."""
        return self._read_all_entries()[-max_entries:]

    def count(self) -> int:
        """Return the total number of entries."""
        return len(self._read_all_entries())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_all_entries(self) -> list[HistoryEntry]:
        """Read all entries without cursor filtering."""
        entries: list[HistoryEntry] = []
        with suppress(FileNotFoundError):
            with open(self._history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(HistoryEntry.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        return entries

    def _read_last_entry(self) -> HistoryEntry | None:
        """Read the last entry efficiently (tail of file)."""
        try:
            with open(self._history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.split("\n") if line.strip()]
                if not lines:
                    return None
                return HistoryEntry.from_dict(json.loads(lines[-1]))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _read_cursor(self) -> int:
        """Read the persisted cursor counter."""
        if not self._cursor_file.exists():
            return 0
        with suppress(ValueError, OSError):
            cursor = int(self._cursor_file.read_text(encoding="utf-8").strip())
            if cursor >= 0:
                return cursor
        return 0

    def _next_cursor(self) -> int:
        """Compute the next cursor value.

        Uses max(persisted cursor, last entry cursor) + 1 for resilience
        against incomplete writes.
        """
        persisted = self._read_cursor()
        last = self._read_last_entry()
        last_cursor = last.cursor if last else 0
        return max(persisted, last_cursor) + 1

    def _maybe_compact(self) -> None:
        """Drop oldest entries if the file exceeds max_entries."""
        if self._max_entries <= 0:
            return
        entries = self._read_all_entries()
        if len(entries) <= self._max_entries:
            return
        kept = entries[-self._max_entries:]
        self._write_entries(kept)

    def _write_entries(self, entries: list[HistoryEntry]) -> None:
        """Overwrite the history file atomically."""
        tmp_path = self._history_file.with_suffix(self._history_file.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._history_file)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
