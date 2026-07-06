"""Long-term, cross-session memory storage.

Memory holds durable facts/preferences that should be visible from
every session (e.g. "用户正在实现一个名为 claw 的课程 agent 项目").
It is only ever changed through explicit `/memory` commands, never by
ordinary chat messages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_MEMORY_ID_PATTERN = re.compile(r"^mem_(\d+)$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStoreError(RuntimeError):
    """Raised for user-facing memory storage failures."""


@dataclass(frozen=True)
class MemoryEntry:
    memory_id: str
    content: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.memory_id,
            "content": self.content,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        try:
            memory_id = data["id"]
            content = data["content"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"memory 数据格式错误，缺少字段: {data!r}") from exc
        created_at = data.get("createdAt") or _now_iso()
        return cls(memory_id=memory_id, content=content, created_at=created_at)


class MemoryStore:
    """Add/list/delete long-term memory entries, persisted as JSON."""

    def __init__(self, memory_file: Path):
        self._memory_file = memory_file
        self._entries: list[MemoryEntry] = []
        self.load_warning: str | None = None
        self._load()

    def _load(self) -> None:
        if not self._memory_file.exists():
            return
        try:
            raw_text = self._memory_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            if not isinstance(data, list):
                raise ValueError("memory 文件格式错误：顶层结构应为数组")
            self._entries = [MemoryEntry.from_dict(item) for item in data]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            backup_path = self._quarantine_corrupted_file()
            self.load_warning = (
                f"memory 文件已损坏，无法解析，已备份为 {backup_path.name}，"
                f"本次将以空 memory 启动（原始数据未被覆盖或删除）。详情：{exc}"
            )
            self._entries = []

    def _quarantine_corrupted_file(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self._memory_file.with_name(
            f"{self._memory_file.name}.corrupted-{timestamp}"
        )
        try:
            self._memory_file.rename(backup_path)
        except OSError:
            return self._memory_file
        return backup_path

    def list(self) -> list[MemoryEntry]:
        return list(self._entries)

    def add(self, content: str) -> MemoryEntry:
        content = content.strip()
        if not content:
            raise MemoryStoreError("memory 内容不能为空")
        entry = MemoryEntry(
            memory_id=self._generate_id(),
            content=content,
            created_at=_now_iso(),
        )
        self._entries.append(entry)
        self._save()
        return entry

    def delete(self, memory_id: str) -> None:
        remaining = [e for e in self._entries if e.memory_id != memory_id]
        if len(remaining) == len(self._entries):
            raise MemoryStoreError(f"未找到 memory: {memory_id}")
        self._entries = remaining
        self._save()

    def _save(self) -> None:
        try:
            self._memory_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self._memory_file.with_suffix(".tmp")
            tmp_file.write_text(
                json.dumps(
                    [entry.to_dict() for entry in self._entries],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp_file.replace(self._memory_file)
        except OSError as exc:
            raise MemoryStoreError(f"保存 memory 失败: {exc}") from exc

    def _generate_id(self) -> str:
        max_seq = 0
        for entry in self._entries:
            match = _MEMORY_ID_PATTERN.match(entry.memory_id)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        return f"mem_{max_seq + 1:03d}"
