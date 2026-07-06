"""Persistent, per-session storage.

Each session is stored as its own JSON file at
`<sessions_dir>/<sessionId>/session.json`, so a session can always be
located directly by its id instead of scanning one giant file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claw.session.models import Session

DEFAULT_SESSION_ID = "default"
DEFAULT_SESSION_TITLE = "默认会话"

_SESSION_ID_PATTERN = re.compile(r"^session_(\d+)$")
_SESSION_FILE_NAME = "session.json"


class SessionStoreError(RuntimeError):
    """Raised for user-facing session storage failures."""


class SessionNotFoundError(SessionStoreError):
    """Raised when a session id does not exist."""


@dataclass(frozen=True)
class SessionSummary:
    """Lightweight metadata used for `/session list`."""

    session_id: str
    title: str
    message_count: int
    updated_at: str


class SessionStore:
    """Create, list, switch, rename, delete and persist sessions."""

    def __init__(self, sessions_dir: Path):
        self._sessions_dir = sessions_dir
        self._cache: dict[str, Session] = {}
        self.load_warnings: list[str] = []
        self._load_all()

    # -- startup loading ----------------------------------------------
    def _load_all(self) -> None:
        self._cache.clear()
        self.load_warnings.clear()
        if not self._sessions_dir.exists():
            return

        for entry in sorted(self._sessions_dir.iterdir()):
            if not entry.is_dir():
                continue
            session_file = entry / _SESSION_FILE_NAME
            if not session_file.exists():
                continue
            try:
                session = self._read_session_file(session_file)
            except SessionStoreError as exc:
                backup_path = self._quarantine_corrupted_file(session_file)
                self.load_warnings.append(
                    f"session `{entry.name}` 的数据文件已损坏，无法解析，"
                    f"已备份为 {backup_path.name} 并跳过加载（原始数据未被覆盖或删除）。"
                    f"详情：{exc}"
                )
                continue
            self._cache[session.session_id] = session

    @staticmethod
    def _read_session_file(path: Path) -> Session:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SessionStoreError(f"无法读取 session 文件 {path}: {exc}") from exc
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SessionStoreError(f"session 文件 JSON 格式损坏: {exc}") from exc
        try:
            return Session.from_dict(data)
        except ValueError as exc:
            raise SessionStoreError(str(exc)) from exc

    @staticmethod
    def _quarantine_corrupted_file(path: Path) -> Path:
        """Rename a corrupted file instead of deleting/overwriting it."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(f"{path.name}.corrupted-{timestamp}")
        try:
            path.rename(backup_path)
        except OSError:
            return path
        return backup_path

    # -- paths ----------------------------------------------------------
    def _session_dir(self, session_id: str) -> Path:
        return self._sessions_dir / session_id

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / _SESSION_FILE_NAME

    # -- CRUD -------------------------------------------------------------
    def ensure_default_session(self) -> Session:
        """Return any existing session, or create the default one."""
        if self._cache:
            return next(iter(self._cache.values()))
        return self.create_session(
            session_id=DEFAULT_SESSION_ID, title=DEFAULT_SESSION_TITLE
        )

    def create_session(
        self, title: str | None = None, session_id: str | None = None
    ) -> Session:
        resolved_id = session_id or self._generate_session_id()
        if resolved_id in self._cache:
            raise SessionStoreError(f"session id 已存在: {resolved_id}")
        session = Session(session_id=resolved_id, title=title or resolved_id)
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

    def list_summaries(self) -> list[SessionSummary]:
        summaries = [
            SessionSummary(
                session_id=session.session_id,
                title=session.title,
                message_count=len(session.messages),
                updated_at=session.updated_at,
            )
            for session in self._cache.values()
        ]
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)

    def rename(self, session_id: str, new_title: str) -> Session:
        session = self.get(session_id)
        session.title = new_title
        session.touch()
        self.save(session)
        return session

    def delete(self, session_id: str) -> None:
        self.get(session_id)  # raises SessionNotFoundError if missing
        del self._cache[session_id]
        try:
            session_file = self._session_file(session_id)
            if session_file.exists():
                session_file.unlink()
            session_dir = self._session_dir(session_id)
            if session_dir.exists() and not any(session_dir.iterdir()):
                session_dir.rmdir()
        except OSError as exc:
            raise SessionStoreError(
                f"删除 session {session_id} 的文件失败: {exc}"
            ) from exc

    def save(self, session: Session) -> None:
        session_dir = self._session_dir(session.session_id)
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            session_file = self._session_file(session.session_id)
            tmp_file = session_file.with_suffix(".tmp")
            tmp_file.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(session_file)
        except OSError as exc:
            raise SessionStoreError(
                f"保存 session {session.session_id} 失败，本轮内容可能未落盘: {exc}"
            ) from exc

    def _generate_session_id(self) -> str:
        max_seq = 0
        for existing_id in self._cache:
            match = _SESSION_ID_PATTERN.match(existing_id)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        return f"session_{max_seq + 1:03d}"
