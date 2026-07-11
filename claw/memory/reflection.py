"""Daily memory reflection: auto-summarise conversations into long-term memory.

A background thread checks once per minute whether the configured daily
time has arrived. When triggered, it:

1. Finds all sessions modified since the last run.
2. Builds a compact prompt (summary + recent messages per session).
3. Calls the LLM to extract structured ``{category, content, tags,
   importance}`` facts.
4. Auto-saves them to ``MemoryStore`` (no approval — system-initiated).
5. Records the run in ``run_history``.

Config is persisted at ``data/memory/reflection_config.json``.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claw.memory.store import MEMORY_CATEGORIES, MemoryStore, MemoryStoreError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REFLECTION_CONFIG_FILE = "reflection_config.json"
_POLL_INTERVAL_SECONDS = 60  # Check every minute whether it's time
_MAX_RUN_HISTORY = 50
_DEFAULT_TIME = "23:00"

_REFLECTION_SYSTEM_PROMPT = (
    "你是一个记忆整理助手。你的任务是回顾用户最近的对话，"
    "从中提取值得长期记忆的关键信息。\n\n"
    "提取规则：\n"
    "1. 只提取有长期价值的信息（项目、偏好、决策、重要事实）。\n"
    "2. 忽略临时的、一次性的问题、调试细节、寒暄。\n"
    "3. 如果某条信息与已有记忆明显重复，不要重复提取。\n"
    "4. 每条记忆用简洁的一句话表达。\n\n"
    "返回格式（纯 JSON 数组，不要任何额外文字）：\n"
    '[\n'
    '  {"category":"project","content":"用户正在开发智能客服系统","tags":["fastapi","postgresql"],"importance":4},\n'
    '  {"category":"user_preference","content":"用户喜欢中文交流","tags":["language"],"importance":3}\n'
    ']\n\n'
    f"category 可选值: {', '.join(sorted(MEMORY_CATEGORIES))}"
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ReflectionRun:
    """A single reflection execution record."""

    run_at: str
    sessions_reviewed: int
    facts_extracted: int
    status: str  # "success" | "partial" | "failure"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "runAt": self.run_at,
            "sessionsReviewed": self.sessions_reviewed,
            "factsExtracted": self.facts_extracted,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class ReflectionConfig:
    """Persistent configuration for the daily reflection task."""

    enabled: bool = True
    time: str = _DEFAULT_TIME  # "HH:MM"
    last_run_at: str = ""  # ISO timestamp or ""
    run_history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "time": self.time,
            "lastRunAt": self.last_run_at or None,
            "runHistory": self.run_history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReflectionConfig":
        history = data.get("runHistory", [])
        if not isinstance(history, list):
            history = []
        return cls(
            enabled=data.get("enabled", True),
            time=data.get("time", _DEFAULT_TIME),
            last_run_at=data.get("lastRunAt") or "",
            run_history=history,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _time_matches(configured_time: str) -> bool:
    """Check whether the current wall-clock time (HH:MM) equals *configured_time*."""
    try:
        now = datetime.now()
        hhmm = f"{now.hour:02d}:{now.minute:02d}"
        return hhmm == configured_time
    except Exception:
        return False


def _same_day(iso1: str, iso2: str) -> bool:
    """Return True if both ISO timestamps refer to the same calendar day."""
    try:
        d1 = datetime.fromisoformat(iso1)
        d2 = datetime.fromisoformat(iso2)
        return d1.date() == d2.date()
    except Exception:
        return False


def _parse_facts_from_response(raw: str) -> list[dict[str, Any]]:
    """Extract a JSON array of memory facts from an LLM response.

    Tolerates markdown code fences and surrounding text.
    """
    if not raw:
        return []

    # Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.strip()

    # Find the outermost JSON array
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end <= start:
        return []

    try:
        parsed = json.loads(cleaned[start : end + 1])
        if not isinstance(parsed, list):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    facts: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cat = item.get("category", "general")
        content = item.get("content", "")
        if not content or cat not in MEMORY_CATEGORIES:
            continue
        tags = item.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        importance = item.get("importance", 3)
        if not isinstance(importance, int) or not (1 <= importance <= 5):
            importance = 3
        facts.append({
            "category": cat,
            "content": str(content).strip(),
            "tags": [str(t).strip().lower() for t in tags if str(t).strip()],
            "importance": importance,
        })
    return facts


# =============================================================================
# ReflectionManager
# =============================================================================


class ReflectionError(RuntimeError):
    """Raised when reflection cannot proceed (non-fatal — logged, not thrown)."""


class ReflectionManager:
    """Background daily reflection: review sessions → extract memories.

    Usage::

        mgr = ReflectionManager(config_dir, memory_store, session_store, llm_client)
        mgr.start()
        ...
        mgr.stop()

        # Manual trigger (CLI / API):
        result = mgr.run_now()
    """

    def __init__(
        self,
        config_dir: Path,
        memory_store: MemoryStore,
        session_store,
        llm_client,
    ):
        self._config_path = config_dir / _REFLECTION_CONFIG_FILE
        self._memory_store = memory_store
        self._session_store = session_store
        self._llm_client = llm_client

        self._config: ReflectionConfig = self._load_config()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._ran_today: str = ""  # ISO date string of the last trigger today

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if not self._config.enabled:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="claw-reflection"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Config accessors (thread-safe)
    # ------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return self._config.to_dict()

    def update_config(self, **kwargs) -> dict[str, Any]:
        """Update reflection config fields. Accepted keys:
        ``enabled`` (bool), ``time`` (str "HH:MM").
        """
        with self._lock:
            if "enabled" in kwargs:
                self._config.enabled = bool(kwargs["enabled"])
            if "time" in kwargs:
                raw = str(kwargs["time"]).strip()
                # Validate HH:MM format
                if re.match(r"^\d{2}:\d{2}$", raw):
                    self._config.time = raw
            self._save_config()

            # Start / stop background thread as needed
            if self._config.enabled and not self._running:
                self._running = True
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="claw-reflection"
                )
                self._thread.start()
            elif not self._config.enabled and self._running:
                self._running = False

            return self._config.to_dict()

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    def run_now(self) -> dict[str, Any]:
        """Execute reflection immediately. Returns a result dict."""
        if self._llm_client is None:
            return {"ok": False, "error": "LLM 客户端未初始化"}

        sessions_reviewed = 0
        facts_extracted = 0
        status = "success"
        error_msg = ""

        try:
            sessions = self._gather_sessions()
            sessions_reviewed = len(sessions)
            facts = self._extract_facts_batch(sessions)
            facts_extracted = self._save_facts(facts)

            now = _now_iso()
            with self._lock:
                self._config.last_run_at = now
                self._ran_today = now[:10]
                self._config.run_history.append(
                    ReflectionRun(
                        run_at=now,
                        sessions_reviewed=sessions_reviewed,
                        facts_extracted=facts_extracted,
                        status=status,
                    ).to_dict()
                )
                if len(self._config.run_history) > _MAX_RUN_HISTORY:
                    self._config.run_history = self._config.run_history[-_MAX_RUN_HISTORY:]
                self._save_config()
        except ReflectionError as exc:
            status = "failure"
            error_msg = str(exc)
            with self._lock:
                now = _now_iso()
                self._config.run_history.append(
                    ReflectionRun(
                        run_at=now,
                        sessions_reviewed=sessions_reviewed,
                        facts_extracted=facts_extracted,
                        status=status,
                        error=error_msg,
                    ).to_dict()
                )
                if len(self._config.run_history) > _MAX_RUN_HISTORY:
                    self._config.run_history = self._config.run_history[-_MAX_RUN_HISTORY:]
                self._save_config()
        except Exception as exc:
            status = "failure"
            error_msg = f"未预期的异常: {exc}"

        return {
            "ok": status != "failure",
            "sessionsReviewed": sessions_reviewed,
            "factsExtracted": facts_extracted,
            "status": status,
            "error": error_msg,
        }

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Polling loop — checks every minute whether the daily time has arrived."""
        while self._running:
            try:
                self._tick()
            except Exception:
                traceback.print_exc()
            time.sleep(_POLL_INTERVAL_SECONDS)

    def _tick(self) -> None:
        with self._lock:
            enabled = self._config.enabled
            target_time = self._config.time
            last_run = self._config.last_run_at

        if not enabled:
            return

        now = _now_iso()

        # Guard: don't run more than once per day
        if last_run and _same_day(now, last_run):
            return

        if _time_matches(target_time):
            # Small delay to avoid double-trigger within the same minute
            with self._lock:
                if self._ran_today == now[:10]:
                    return
            self.run_now()

    # ------------------------------------------------------------------
    # Internal: session gathering
    # ------------------------------------------------------------------

    def _gather_sessions(self) -> list[dict[str, Any]]:
        """Return lightweight session snapshots for sessions with messages."""
        last_run = self._config.last_run_at
        summaries = self._session_store.list_summaries()

        sessions: list[dict[str, Any]] = []
        for s in summaries:
            # Skip sessions with no messages
            if s.message_count == 0:
                continue
            # If we have a last_run_at, only include sessions modified since then
            if last_run and s.updated_at <= last_run:
                continue

            try:
                session = self._session_store.get(s.session_id)
            except Exception:
                continue

            # Build a compact view: summary + recent messages
            parts: list[str] = []
            if session.summary.strip():
                parts.append(f"已有摘要: {session.summary.strip()}")

            # Include last ~20 messages (keep it within reasonable token budget)
            recent = session.messages[-20:] if len(session.messages) > 20 else session.messages
            if recent:
                msg_lines = []
                for m in recent:
                    role_icon = {"user": "👤", "assistant": "🤖", "tool": "🔧"}.get(m.role, m.role)
                    # Truncate long messages
                    content = m.content[:300] + ("..." if len(m.content) > 300 else "")
                    msg_lines.append(f"{role_icon} [{m.role}] {content}")
                parts.append("最近对话:\n" + "\n".join(msg_lines))

            sessions.append({
                "session_id": s.session_id,
                "title": s.title,
                "message_count": s.message_count,
                "context": "\n\n".join(parts),
            })

        return sessions

    # ------------------------------------------------------------------
    # Internal: LLM extraction
    # ------------------------------------------------------------------

    def _extract_facts_batch(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Call the LLM once with all sessions' context, return parsed facts."""
        if not sessions:
            return []

        # Build a single user message with all sessions
        existing_memories = self._memory_store.list()
        existing_block = ""
        if existing_memories:
            existing_lines = "\n".join(
                f"- [{e.category}] {e.content}" for e in existing_memories
            )
            existing_block = (
                "## 已有记忆（避免重复提取）\n" + existing_lines + "\n\n"
            )

        session_blocks: list[str] = []
        for s in sessions:
            block = (
                f"### Session: {s['session_id']} ({s['title']})\n"
                f"消息数: {s['message_count']}\n\n"
                f"{s['context']}"
            )
            session_blocks.append(block)

        user_content = (
            f"{existing_block}"
            f"## 待整理的 Session（共 {len(sessions)} 个）\n\n"
            + "\n\n---\n\n".join(session_blocks)
            + "\n\n请提取值得长期记忆的关键信息，以 JSON 数组返回。"
        )

        messages = [
            {"role": "system", "content": _REFLECTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        try:
            raw = self._llm_client.chat(messages)
            return _parse_facts_from_response(raw)
        except Exception as exc:
            raise ReflectionError(f"LLM 调用失败: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal: save facts
    # ------------------------------------------------------------------

    def _save_facts(self, facts: list[dict[str, Any]]) -> int:
        """Save extracted facts to MemoryStore. Returns count of saved facts."""
        saved = 0
        for fact in facts:
            try:
                self._memory_store.add(
                    content=fact["content"],
                    category=fact["category"],
                    tags=fact.get("tags", []),
                    importance=fact.get("importance", 3),
                    source_session_id="reflection",
                )
                saved += 1
            except MemoryStoreError:
                # Skip invalid facts; don't fail the whole batch
                continue
        return saved

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def _load_config(self) -> ReflectionConfig:
        if not self._config_path.exists():
            return ReflectionConfig()
        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return ReflectionConfig.from_dict(data)
        except (OSError, json.JSONDecodeError):
            pass
        return ReflectionConfig()

    def _save_config(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._config_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._config.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._config_path)
        except OSError:
            pass  # Non-fatal — config is readable from memory
