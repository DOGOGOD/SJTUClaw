"""Long-term, cross-session memory storage — Hermes-style Markdown files.

Each memory is a standalone ``.md`` file under ``data/memory/<category>/``,
using YAML frontmatter for structured metadata and Markdown body for rich
content.  The filesystem *is* the database — memories are human-readable,
editor-friendly, and git-trackable.

Startup:
    1. Scan ``data/memory/`` for ``*/*.md`` files.
    2. Parse YAML frontmatter + body into ``MemoryEntry`` objects.
    3. If an old ``memory.json`` exists and no .md files were found,
       auto-migrate: convert each JSON entry into a .md file, then
       rename ``memory.json`` → ``memory.json.migrated``.

Persistence:
    ``add()``       → writes a new .md file (atomic tmp+replace).
    ``update()``    → rewrites the existing .md file.
    ``delete()``    → removes the .md file.
    ``recall()``    → scans in-memory entries (no disk I/O).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_ID_PATTERN = re.compile(r"^mem_(\d+)$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)

MEMORY_CATEGORIES = frozenset({
    "user_preference",
    "project",
    "decision",
    "fact",
    "general",
})

_CATEGORY_LABELS: dict[str, str] = {
    "user_preference": "用户偏好",
    "project": "项目信息",
    "decision": "决策记录",
    "fact": "一般事实",
    "general": "其他",
}

_LEGACY_FILE_NAME = "memory.json"
_MIGRATED_SUFFIX = ".migrated"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(text: str, max_len: int = 50) -> str:
    """Turn *text* into a URL-safe filename slug."""
    slug = text[:max_len].strip().lower()
    # Replace runs of non-alphanumeric chars with a single dash
    slug = re.sub(r"[^\w一-鿿]+", "-", slug).strip("-")
    return slug or "memory"


def _extract_cjk_chars(text: str) -> list[str]:
    """Return individual CJK characters from *text* (for character-level matching)."""
    chars: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            chars.append(ch)
    return chars


# ---------------------------------------------------------------------------
# Lightweight YAML frontmatter parser (no PyYAML dependency)
# ---------------------------------------------------------------------------


def _parse_yaml_frontmatter(fm_text: str) -> dict:
    """Parse a minimal YAML frontmatter block into a dict.

    Supports:
        - ``key: value`` (string / int)
        - YAML list: ``key:\n  - item1\n  - item2``
        - inline list: ``key: [item1, item2]``
    """
    result: dict = {}
    key: str | None = None
    list_values: list[str] = []

    def _flush():
        nonlocal key, list_values
        if key is not None:
            if list_values:
                result[key] = list_values
            list_values = []
            key = None

    for line in fm_text.splitlines():
        if not line.strip():
            _flush()
            continue

        # List continuation: "  - item"
        list_match = re.match(r"^  -\s+(.*)", line)
        if list_match and key is not None:
            list_values.append(list_match.group(1).strip().strip("'\""))
            continue

        _flush()

        # key: value
        kv = re.match(r"^(\w[\w-]*)\s*:\s*(.*)", line)
        if kv:
            key = kv.group(1)
            raw_val = kv.group(2).strip()

            # Inline list: [a, b, c]
            if raw_val.startswith("[") and raw_val.endswith("]"):
                items = re.findall(r"[\"']?([^\"'\]\[,]+)[\"']?", raw_val)
                result[key] = [it.strip() for it in items if it.strip()]
                key = None
                continue

            # Empty value → may be start of block list
            if not raw_val:
                list_values = []
                continue

            # Scalar value
            val = raw_val.strip().strip("'\"")
            # Try int
            try:
                result[key] = int(val)
            except ValueError:
                result[key] = val
            key = None

    _flush()
    return result


def _format_yaml_frontmatter(meta: dict) -> str:
    """Format a metadata dict as a YAML frontmatter string."""
    lines = ["---"]
    # id always first
    if "id" in meta:
        lines.append(f'id: "{meta["id"]}"')

    for k, v in meta.items():
        if k == "id":
            continue
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"  - {item}")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        elif isinstance(v, str) and v:
            lines.append(f'{k}: "{v}"')
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class MemoryStoreError(RuntimeError):
    """User-facing memory storage error."""


@dataclass
class MemoryEntry:
    """A single structured memory — metadata + rich Markdown body."""

    memory_id: str
    content: str          # The Markdown body (may include headers, lists, etc.)
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    importance: int = 3
    source_session_id: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_recalled_at: str = ""
    recall_count: int = 0

    # Path to the backing .md file (set by MemoryStore after load/add)
    _file_path: Path | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.category not in MEMORY_CATEGORIES:
            raise MemoryStoreError(
                f"无效的记忆类别: \"{self.category}\"，"
                f"可选: {', '.join(sorted(MEMORY_CATEGORIES))}"
            )
        if not 1 <= self.importance <= 5:
            raise MemoryStoreError(
                f"importance 必须在 1-5 之间，实际为 {self.importance}"
            )
        # Normalise tags
        seen: set[str] = set()
        norm: list[str] = []
        for t in self.tags:
            t = t.strip().lower()
            if t and t not in seen:
                seen.add(t)
                norm.append(t)
        self.tags = sorted(norm)

    # -- serialisation (frontmatter dict + body) -----------------------------

    def to_frontmatter_dict(self) -> dict:
        return {
            "id": self.memory_id,
            "category": self.category,
            "tags": self.tags,
            "importance": self.importance,
            "source_session_id": self.source_session_id or "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_recalled_at": self.last_recalled_at or "",
            "recall_count": self.recall_count,
        }

    def to_markdown(self) -> str:
        """Render the full .md file content."""
        meta = self.to_frontmatter_dict()
        # Put id first
        ordered = {"id": meta.pop("id"), **meta}
        yaml_block = _format_yaml_frontmatter(ordered)
        body = self.content.strip()
        return f"{yaml_block}\n---\n\n{body}\n"

    @classmethod
    def from_markdown(cls, raw: str, file_path: Path | None = None) -> "MemoryEntry":
        """Parse a .md file into a MemoryEntry."""
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            raise MemoryStoreError(
                f"记忆文件格式错误——缺少 YAML frontmatter (--- 分隔符): {file_path}"
            )

        meta = _parse_yaml_frontmatter(m.group(1))
        body = m.group(2).strip()

        memory_id = meta.get("id", "")
        if not memory_id:
            raise MemoryStoreError(f"frontmatter 缺少 id 字段: {file_path}")

        created_at = meta.get("created_at") or _now_iso()

        return cls(
            memory_id=memory_id,
            content=body,
            category=meta.get("category", "general"),
            tags=meta.get("tags") if isinstance(meta.get("tags"), list) else [],
            importance=int(meta.get("importance", 3)),
            source_session_id=str(meta.get("source_session_id", "")),
            created_at=str(created_at),
            updated_at=str(meta.get("updated_at") or created_at),
            last_recalled_at=str(meta.get("last_recalled_at", "")),
            recall_count=int(meta.get("recall_count", 0)),
            _file_path=file_path,
        )

    # -- legacy JSON compat -------------------------------------------------

    def to_legacy_dict(self) -> dict:
        return {
            "id": self.memory_id,
            "content": self.content,
            "category": self.category,
            "tags": self.tags,
            "importance": self.importance,
            "sourceSessionId": self.source_session_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastRecalledAt": self.last_recalled_at,
            "recallCount": self.recall_count,
        }

    @classmethod
    def from_legacy_dict(cls, data: dict) -> "MemoryEntry":
        """Deserialise from old JSON format."""
        memory_id = data.get("id", "")
        content = data.get("content", "")
        created_at = data.get("createdAt") or _now_iso()
        return cls(
            memory_id=memory_id,
            content=content,
            category=data.get("category", "general"),
            tags=data.get("tags") if isinstance(data.get("tags"), list) else [],
            importance=data.get("importance", 3),
            source_session_id=data.get("sourceSessionId", ""),
            created_at=str(created_at),
            updated_at=str(data.get("updatedAt") or created_at),
            last_recalled_at=str(data.get("lastRecalledAt", "")),
            recall_count=data.get("recallCount", 0),
        )


# =============================================================================
# MemoryStore (Hermes-style Markdown-backed)
# =============================================================================


class MemoryStore:
    """Markdown-file-backed memory store.

    Directory layout::

        data/memory/
        ├── user_preference/
        │   └── <slug>.md
        ├── project/
        │   └── <slug>.md
        ├── fact/
        │   └── <slug>.md
        ├── decision/
        │   └── <slug>.md
        └── general/
            └── <slug>.md

    On startup all .md files are scanned and parsed into an in-memory
    index.  ``recall()`` searches this index — no disk I/O on retrieval.
    """

    def __init__(self, memory_dir: Path):
        self._memory_dir = Path(memory_dir)
        self._entries: list[MemoryEntry] = []
        self.load_warning: str | None = None
        self._load()

    # ------------------------------------------------------------------
    # Startup: scan .md files + optional legacy migration
    # ------------------------------------------------------------------

    def _load(self) -> None:
        self._entries.clear()

        # Ensure root dir exists
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        # Scan for .md files
        md_files = sorted(self._memory_dir.glob("*/*.md"))
        for md_path in md_files:
            try:
                raw = md_path.read_text(encoding="utf-8")
                entry = MemoryEntry.from_markdown(raw, file_path=md_path)
                self._entries.append(entry)
            except MemoryStoreError as exc:
                print(f"[memory] 警告: 跳过损坏的记忆文件 {md_path}: {exc}")

        # Legacy migration: memory.json → .md files
        legacy_path = self._memory_dir / _LEGACY_FILE_NAME
        if legacy_path.exists() and not md_files:
            self._migrate_from_legacy(legacy_path)

    def _migrate_from_legacy(self, legacy_path: Path) -> None:
        """Convert old ``memory.json`` entries into .md files."""
        try:
            raw = legacy_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("不是 JSON 数组")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.load_warning = (
                f"旧 memory.json 无法解析，跳过迁移。详情: {exc}"
            )
            return

        migrated = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                entry = MemoryEntry.from_legacy_dict(item)
                self._write_md_file(entry)
                self._entries.append(entry)
                migrated += 1
            except MemoryStoreError as exc:
                print(f"[memory] 迁移跳过条目 {item.get('id', '?')}: {exc}")

        # Rename legacy file so it won't be picked up again
        migrated_path = legacy_path.with_name(
            f"{_LEGACY_FILE_NAME}{_MIGRATED_SUFFIX}"
        )
        try:
            legacy_path.rename(migrated_path)
        except OSError:
            pass

        print(f"[memory] 已从 memory.json 迁移 {migrated} 条记忆到 Markdown 文件")

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _category_dir(self, category: str) -> Path:
        d = self._memory_dir / category
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _make_file_path(self, entry: MemoryEntry) -> Path:
        """Determine the .md file path for *entry*."""
        slug = _slugify(entry.content)
        base = self._category_dir(entry.category) / f"{slug}.md"

        # If the file already exists for a DIFFERENT entry, append a number
        if base.exists():
            # Check if it's owned by this entry
            try:
                raw = base.read_text(encoding="utf-8")
                existing = MemoryEntry.from_markdown(raw, file_path=base)
                if existing.memory_id == entry.memory_id:
                    return base
            except MemoryStoreError:
                pass
            # Conflict — find a free name
            counter = 2
            while True:
                cand = self._category_dir(entry.category) / f"{slug}-{counter}.md"
                if not cand.exists():
                    return cand
                counter += 1
        return base

    def _write_md_file(self, entry: MemoryEntry) -> Path:
        """Write *entry* to its .md file (atomic tmp+replace)."""
        file_path = self._make_file_path(entry)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = file_path.with_suffix(".tmp")
        tmp.write_text(entry.to_markdown(), encoding="utf-8")
        tmp.replace(file_path)
        entry._file_path = file_path
        return file_path

    def _delete_md_file(self, entry: MemoryEntry) -> None:
        """Remove the backing .md file, if it exists."""
        if entry._file_path and entry._file_path.exists():
            try:
                entry._file_path.unlink()
            except OSError:
                pass
        # Clean up empty category dir
        if entry._file_path:
            parent = entry._file_path.parent
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # CRUD — basic
    # ------------------------------------------------------------------

    def list(self) -> list[MemoryEntry]:
        return list(self._entries)

    def add(
        self,
        content: str,
        *,
        category: str = "general",
        tags: list[str] | None = None,
        importance: int = 3,
        source_session_id: str = "",
    ) -> MemoryEntry:
        content = content.strip()
        if not content:
            raise MemoryStoreError("memory 内容不能为空")

        now = _now_iso()
        entry = MemoryEntry(
            memory_id=self._generate_id(),
            content=content,
            category=category,
            tags=tags or [],
            importance=importance,
            source_session_id=source_session_id,
            created_at=now,
            updated_at=now,
        )
        self._write_md_file(entry)
        self._entries.append(entry)
        return entry

    def update(self, memory_id: str, content: str) -> MemoryEntry:
        content = content.strip()
        if not content:
            raise MemoryStoreError("memory 内容不能为空")

        for entry in self._entries:
            if entry.memory_id == memory_id:
                # Remove old file if the slug changed
                old_path = entry._file_path
                entry.content = content
                entry.updated_at = _now_iso()
                new_path = self._write_md_file(entry)
                if old_path and old_path.exists() and old_path != new_path:
                    try:
                        old_path.unlink()
                    except OSError:
                        pass
                return entry

        raise MemoryStoreError(f"未找到 memory: {memory_id}")

    def delete(self, memory_id: str) -> None:
        for i, entry in enumerate(self._entries):
            if entry.memory_id == memory_id:
                self._delete_md_file(entry)
                self._entries.pop(i)
                return
        raise MemoryStoreError(f"未找到 memory: {memory_id}")

    # ------------------------------------------------------------------
    # Structured operations
    # ------------------------------------------------------------------

    def list_by_category(self, category: str | None = None) -> list[MemoryEntry]:
        if category is None:
            return self.list()
        return [e for e in self._entries if e.category == category]

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries:
            label = _CATEGORY_LABELS.get(entry.category, entry.category)
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))

    # ------------------------------------------------------------------
    # Retrieval (keyword + tag + CJK char scoring)
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        limit = max(1, min(limit, 20))
        query_lower = query.lower()
        query_terms = [t for t in query_lower.split() if len(t) >= 2]
        query_cjk_chars = _extract_cjk_chars(query_lower)

        scored: list[tuple[float, MemoryEntry]] = []

        for entry in self._entries:
            if category and entry.category != category:
                continue

            score = 0.0

            # 1. Tag matching
            for tag in entry.tags:
                tag_lower = tag.lower()
                if tag_lower in query_lower or query_lower in tag_lower:
                    score += 10.0
                else:
                    for term in query_terms:
                        if term in tag_lower:
                            score += 5.0
                            break

            # 2. Content substring / term matching
            content_lower = entry.content.lower()
            if query_lower in content_lower:
                score += 8.0
            for term in query_terms:
                if term in content_lower:
                    score += 3.0

            # 3. CJK character-level matching
            if query_cjk_chars and score == 0:
                content_cjk = set(_extract_cjk_chars(content_lower))
                matched = [c for c in query_cjk_chars if c in content_cjk]
                if matched:
                    score += min(len(matched) / len(query_cjk_chars) * 6.0, 6.0)

            # 4. Boosts (only when there is a base match)
            if score > 0:
                if entry.category == "user_preference":
                    score += 2.0
                score += float(entry.importance)
                try:
                    created_dt = datetime.fromisoformat(entry.created_at)
                    age_days = (datetime.now(timezone.utc) - created_dt).days
                    if age_days <= 7:
                        score += 1.0
                except (ValueError, TypeError):
                    pass

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [entry for _, entry in scored[:limit]]

        # Update recall tracking + persist to .md files
        if results:
            now = _now_iso()
            for entry in results:
                entry.last_recalled_at = now
                entry.recall_count += 1
                # Write updated frontmatter back to disk
                try:
                    if entry._file_path:
                        self._write_md_file(entry)
                except Exception:
                    pass  # Non-fatal — tracking lost but data safe

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _generate_id(self) -> str:
        max_seq = 0
        for entry in self._entries:
            match = _MEMORY_ID_PATTERN.match(entry.memory_id)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        return f"mem_{max_seq + 1:03d}"
