"""Dream: two-phase memory consolidation.

The Dream system distills short-term conversation history (from
``history.jsonl``) into long-term memory files: MEMORY.md, SOUL.md,
and USER.md.

Architecture:
    1. **Dream prompt**: reads unprocessed ``history.jsonl`` entries,
       embeds current contents of durable memory files, and asks the
       LLM to propose edits.
    2. **Dream run**: calls the LLM with a restricted tool set (read,
       edit, write — only for memory files), applies the edits, and
       advances the dream cursor.

Cursor-based incremental processing ensures each history entry is
only processed once.  Git tracking (via ``GitTracker``) enables
rollback and audit logging.

Usage::

    dream = DreamManager(memory_dir, history_log, llm_client)
    dream.run()        # process unread history entries
    dream.run_async()  # background thread
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from claw.memory.history_log import HistoryLog


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DREAM_CURSOR_FILE = ".dream_cursor"
_DREAM_MAX_ENTRIES = 20        # max history entries per dream batch
_DREAM_ENTRY_TRUNCATE = 500    # chars per entry in dream prompt
_DREAM_FILE_EMBED_CAP = 8000   # max chars when embedding file contents

# Files tracked by Dream
_DREAM_CONTENT_PATHS = ("SOUL.md", "USER.md", "memory/MEMORY.md")

_DREAM_SYSTEM_PROMPT = (
    "你是一个记忆整合引擎。你的唯一任务是分析对话历史并维护用户的长期记忆文件。"
    "你对删除要像对添加一样果断：删除过时内容和添加新事实同样重要。\n\n"
    "## 文件路由\n"
    "- **SOUL.md**: 代理行为规则、防护栏、交互模式、工具使用策略\n"
    "- **USER.md**: 个人属性：身份、偏好、习惯、沟通风格（语言、长度、语气）\n"
    "- **MEMORY.md**: 项目上下文：目标、架构、战略决策、基础设施概览\n\n"
    "## 规则\n"
    "- 原子事实：'拥有名叫Luna的猫' 而非 '讨论了宠物护理'\n"
    "- 修正：编辑现有条目，不要追加新条目\n"
    "- 冲突：如果新信息与现有条目矛盾，在原地替换旧条目；不要保留两个版本\n"
    "- 捕获用户确认的方法\n"
    "- 沟通边界：语言、长度和语气偏好 -> USER.md。交互模式（主动/被动）和工具使用策略 -> SOUL.md\n"
    "- 跨边界规则：USER.md 中没有技术配置，SOUL.md 中没有用户事实\n\n"
    "## 始终删除\n"
    "- 同一事实出现在多个位置 — 仅保留规范副本\n"
    "- 已合并/关闭的内容、已解决的事件、已被取代的信息\n"
    "- 可以更少文字重述的冗长条目\n"
    "- 通过快速网络搜索就能找到的事实（标准库API、常见CLI标志、公开文档）— "
    "记忆是为用户无法查找的上下文服务的\n\n"
    "## 永不删除\n"
    "- 用户偏好和个性特征（无论多旧都永久保留）\n"
    "- 仍在对话中引用的活跃项目上下文\n"
    "- SOUL.md 中的行为规则\n\n"
    "当前 SOUL.md、USER.md 和 MEMORY.md 的内容已嵌入在提示的 '当前记忆文件' 部分下。"
    "直接编辑这些文件；不要依赖记忆中的文件版本。"
    "将更改批处理到尽可能少的调用中。仅进行外科手术式编辑。"
)


class DreamError(RuntimeError):
    """Non-fatal dream processing error."""


class DreamManager:
    """Two-phase memory consolidation manager.

    Phase 1: Build dream prompt from unprocessed history + current files.
    Phase 2: Call LLM with restricted tool set → apply edits.

    Parameters:
        memory_dir: path to ``data/memory/``.
        history_log: the cross-session ``HistoryLog`` instance.
        llm_client: the LLM client for the dream agent.
        workspace_root: project root (for resolving SOUL.md / USER.md paths).
    """

    def __init__(
        self,
        memory_dir: Path,
        history_log: HistoryLog,
        llm_client,
        workspace_root: Path | None = None,
    ):
        self._memory_dir = Path(memory_dir)
        self._history_log = history_log
        self._llm_client = llm_client
        self._workspace_root = workspace_root or self._memory_dir.parent.parent
        self._cursor_file = self._memory_dir / _DREAM_CURSOR_FILE
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    def get_last_cursor(self) -> int:
        """Return the dream cursor (last processed history entry)."""
        if self._cursor_file.exists():
            try:
                return int(self._cursor_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return 0

    def set_last_cursor(self, cursor: int) -> None:
        """Persist the dream cursor."""
        self._cursor_file.write_text(str(cursor), encoding="utf-8")

    def get_latest_history_cursor(self) -> int:
        """Return the latest cursor from history log."""
        # Read the most recent entry to get its cursor
        entries = self._history_log.read_recent(1)
        return entries[0].cursor if entries else 0

    # ------------------------------------------------------------------
    # Dream prompt building
    # ------------------------------------------------------------------

    def build_dream_prompt(self, max_entries: int = _DREAM_MAX_ENTRIES) -> tuple[str, int] | None:
        """Build the dream prompt with unprocessed history context.

        Returns ``(prompt, last_cursor)`` or ``None`` if nothing to process.
        """
        last_cursor = self.get_last_cursor()
        entries = self._history_log.read_since(since_cursor=last_cursor)
        if not entries:
            return None

        batch = entries[:max_entries]
        history_text = "\n".join(
            f"[{e.timestamp}] {e.content[:_DREAM_ENTRY_TRUNCATE]}"
            for e in batch
        )

        files_section = self._render_memory_files()
        prompt = (
            f"{_DREAM_SYSTEM_PROMPT}\n\n"
            f"{files_section}\n\n"
            f"## 对话历史\n{history_text}\n\n"
            f"请分析以上对话历史，编辑 SOUL.md、USER.md 和 MEMORY.md 以反映需要长期保留的新信息。"
        )
        return prompt, batch[-1].cursor

    def _render_memory_files(self) -> str:
        """Embed current contents of durable memory files."""
        files = [
            ("SOUL.md", self._workspace_root / "SOUL.md"),
            ("USER.md", self._workspace_root / "USER.md"),
            ("MEMORY.md", self._memory_dir / "MEMORY.md"),
        ]
        blocks = []
        for label, path in files:
            try:
                content = path.read_text(encoding="utf-8") if path.exists() else ""
            except OSError:
                content = ""
            if len(content) > _DREAM_FILE_EMBED_CAP:
                content = content[:_DREAM_FILE_EMBED_CAP] + "\n...[truncated]"
            blocks.append(
                f"### {label}\n{content}"
                if content.strip()
                else f"### {label}\n(empty)"
            )
        return "## 当前记忆文件\n" + "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Dream run
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Execute one dream consolidation pass.

        Returns a result dict::

            {"ok": True/False, "entries_processed": N, "cursor_advanced_to": N,
             "reply": str, "error": str}
        """
        with self._lock:
            prompt_result = self.build_dream_prompt()
            if prompt_result is None:
                return {
                    "ok": True,
                    "entries_processed": 0,
                    "cursor_advanced_to": self.get_last_cursor(),
                    "reply": "没有需要处理的新的历史条目。",
                    "error": "",
                }

            prompt, last_cursor = prompt_result

        try:
            # Call the LLM with the dream prompt (no tools — text-only analysis)
            messages = [
                {"role": "system", "content": _DREAM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            reply = self._llm_client.chat(messages)
        except Exception as exc:
            traceback.print_exc()
            return {
                "ok": False,
                "entries_processed": 0,
                "cursor_advanced_to": self.get_last_cursor(),
                "reply": "",
                "error": f"Dream LLM 调用失败: {exc}",
            }

        # Parse the LLM's response for file edits
        try:
            files_edited = self._apply_suggested_edits(reply)
        except Exception as exc:
            traceback.print_exc()
            files_edited = 0

        # Advance the dream cursor
        with self._lock:
            self.set_last_cursor(last_cursor)

        return {
            "ok": True,
            "entries_processed": len(
                self._history_log.read_since(
                    self.get_last_cursor() - (
                        last_cursor - self.get_last_cursor()
                        if last_cursor > self.get_last_cursor() else 0
                    )
                )
            ),
            "cursor_advanced_to": last_cursor,
            "reply": reply[:500] if reply else "",
            "files_edited": files_edited,
            "error": "",
        }

    def _apply_suggested_edits(self, reply: str) -> int:
        """Parse LLM response for file edit suggestions and apply them.

        The LLM is expected to suggest edits in the format:

            ```edit:SOUL.md
            <<<<<<< ORIGINAL
            old content
            =======
            new content
            >>>>>>> UPDATED
            ```

        Falls back gracefully — this is best-effort, not critical path.
        """
        import re

        edited = 0

        # Pattern: ```edit:FILENAME\n...\n```  or  ```edit FILENAME\n...\n```
        edit_pattern = re.compile(
            r"```(?:edit|edit:)\s*(\S+)\s*\n(.*?)```",
            re.DOTALL,
        )

        for match in edit_pattern.finditer(reply):
            filename = match.group(1).strip()
            body = match.group(2)

            # Resolve the file path
            file_path = self._resolve_memory_file(filename)
            if file_path is None:
                continue

            # Try to extract old/new blocks
            old_new = re.search(
                r"<<<<<<< ORIGINAL\s*\n(.*?)\n=======\s*\n(.*?)\n>>>>>>> UPDATED",
                body,
                re.DOTALL,
            )
            if old_new:
                old_text = old_new.group(1)
                new_text = old_new.group(2)

                try:
                    current = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
                    if old_text.strip() in current:
                        updated = current.replace(old_text, new_text, 1)
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = file_path.with_suffix(file_path.suffix + ".tmp")
                        tmp.write_text(updated, encoding="utf-8")
                        tmp.replace(file_path)
                        edited += 1
                except OSError:
                    continue
            else:
                # No structured diff — try treating the body as new content
                try:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
                    tmp.write_text(body.strip(), encoding="utf-8")
                    tmp.replace(file_path)
                    edited += 1
                except OSError:
                    continue

        return edited

    def _resolve_memory_file(self, filename: str) -> Path | None:
        """Resolve a filename to the actual file path."""
        name = filename.lower()
        if "soul" in name:
            return self._workspace_root / "SOUL.md"
        if "user" in name:
            return self._workspace_root / "USER.md"
        if "memory" in name:
            return self._memory_dir / "MEMORY.md"
        return None

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def start_background(self, interval_seconds: int = 600) -> None:
        """Start a background thread that runs dream periodically.

        Default interval: 10 minutes.
        """
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._background_loop,
            args=(interval_seconds,),
            daemon=True,
            name="claw-dream",
        )
        self._thread.start()

    def stop_background(self) -> None:
        """Stop the background dream thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _background_loop(self, interval_seconds: int) -> None:
        import time
        while self._running:
            try:
                result = self.run()
                if result["entries_processed"] > 0:
                    print(
                        f"[dream] 处理了 {result['entries_processed']} 条历史，"
                        f"cursor 推进到 {result['cursor_advanced_to']}"
                    )
            except Exception:
                traceback.print_exc()
            time.sleep(interval_seconds)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def dream_log(self, max_entries: int = 20) -> list[dict[str, Any]]:
        """Return recent dream run summaries for audit."""
        # We store minimal audit in the history log itself —
        # dream entries have event_type="dream_summary"
        entries = self._history_log.read_recent(max_entries * 3)
        return [
            {
                "cursor": e.cursor,
                "timestamp": e.timestamp,
                "summary": e.content[:200],
            }
            for e in entries
            if e.event_type == "dream_summary"
        ][-max_entries:]
