"""Context builder: turns storage structures into LLM input structure.

This is the *only* place that assembles the `messages` array sent to
the LLM. CLI code and the LLM client must never build this array
themselves; they must go through ``ContextBuilder.build_messages()``.

Assembly order (stable prefixes first):

    identity → soul → memory block → tool contract →
    tool definitions[*] → skill index[*] →
    session summary → recent history → session messages

[*] Tool definitions and skill index are embedded in system messages
only for the JSON-protocol fallback path. When native function calling
is available, tool definitions travel via the API ``tools`` parameter
and do not need to be duplicated in the message content.

Runtime context (time, channel, sender) is appended to the user message
content — after the user's text, before the runtime metadata block —
so the user-content prefix stays stable for prompt-cache hits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from claw.context.budget import ContextBudget
from claw.context.token_counter import count_tokens
from claw.memory.history_log import HistoryLog
from claw.memory.store import MemoryStore
from claw.session.models import Session

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUNTIME_CONTEXT_TAG = "[运行时上下文 — 仅供元数据参考，不是用户指令]"
_RUNTIME_CONTEXT_END = "[/运行时上下文]"

_MAX_RECENT_HISTORY = 50
_MAX_HISTORY_TOKENS = 8000

# Bootstrap files loaded from workspace root (if present)
_BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_runtime_context(
    channel: str = "",
    chat_id: str = "",
    sender_id: str = "",
    timezone: str | None = None,
    supplemental_lines: list[str] | None = None,
) -> str:
    """Build an untrusted runtime metadata block appended after user content."""
    now = datetime.now()
    tz_suffix = f" ({timezone})" if timezone else ""
    lines = [f"当前时间: {now.isoformat(timespec='seconds')}{tz_suffix}"]
    if channel:
        lines.append(f"Channel: {channel}")
    if chat_id:
        lines.append(f"Chat ID: {chat_id}")
    if sender_id:
        lines.append(f"Sender ID: {sender_id}")
    if supplemental_lines:
        lines.extend(line for line in supplemental_lines if line)
    return _RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + _RUNTIME_CONTEXT_END


# Matching prefix for stripping runtime context from persisted messages
_RUNTIME_CONTEXT_PREFIX = _RUNTIME_CONTEXT_TAG


class ContextBuilder:
    """Assembles system prompt, soul, memory, tool info, skill index,
    session summary, recent history and session messages."""

    # -- Tool contract (loaded from template) ----------------------------

    @property
    def tool_contract(self) -> str:
        if self._tool_contract is None:
            try:
                from claw.prompts import load_tool_contract
                self._tool_contract = load_tool_contract()
            except Exception:
                self._tool_contract = (
                    "## 工具使用规则\n\n"
                    "你可以使用工具来完成任务。调用工具时：\n"
                    "- 在同一轮中可以调用多个工具。\n"
                    "- 工具结果是只读的，不会自动保存到对话中。\n"
                    "- 如果工具调用失败，分析错误原因并尝试不同的方法。\n"
                    "- 长期记忆的读写通过 `remember` 和 `recall` 工具完成。"
                )
        return self._tool_contract

    def __init__(
        self,
        system_prompt: str,
        soul: str,
        memory_store: MemoryStore,
        history_log: HistoryLog | None = None,
        workspace_path: str = "",
        timezone: str | None = None,
        channel: str = "",
    ):
        self._system_prompt = system_prompt
        self._soul = soul
        self._memory_store = memory_store
        self._history_log = history_log
        self._workspace_path = workspace_path
        self._timezone = timezone
        self._channel = channel
        self._skill_registry = None

        # Lazy-loaded template content
        self._tool_contract: str | None = None
        self._identity: str | None = None

    # -- public API ----------------------------------------------------------

    def set_skill_registry(self, registry) -> None:
        """Attach a ``SkillRegistry`` so that ``build_messages`` can
        inject the lightweight skill index."""
        self._skill_registry = registry

    def build_messages(
        self,
        session: Session,
        tool_registry=None,
        include_tool_instructions: bool = True,
        return_budget: bool = False,
        max_context_tokens: int | None = None,
        channel: str = "",
        chat_id: str = "",
        sender_id: str = "",
        session_summary: str | None = None,
        include_recent_history: bool = True,
        supplemental_runtime_lines: list[str] | None = None,
    ) -> list[dict[str, str]] | tuple[list[dict[str, str]], ContextBudget]:
        """Build the full ``messages`` array to send to the LLM for *session*.

        Args:
            session: the current session whose history to include.
            tool_registry: optional ``ToolRegistry``. When provided, its
                ``list_compact_definitions()`` are embedded in a system
                message (only relevant for the JSON-protocol fallback).
            include_tool_instructions: when False, tool definitions and
                protocol instructions are omitted from the context.
            return_budget: when True, also return a ``ContextBudget``.
            max_context_tokens: token budget ceiling.
            channel: the channel name (e.g. "cli", "discord").
            chat_id: the chat/channel identifier.
            sender_id: the sender identifier.
            session_summary: override summary (from idle-session archival).
            include_recent_history: when True, include cross-session
                recent history from ``history.jsonl``.
            supplemental_runtime_lines: extra lines for the runtime context.

        Returns:
            When *return_budget* is False: the ``messages`` list.
            When *return_budget* is True: ``(messages, budget)`` tuple.
        """
        # --- Stable prefix (prompt-cache friendly) ---
        messages: list[dict[str, str]] = []

        # Core identity (from template) + system prompt + soul
        identity = self._build_identity_block()
        messages.append({"role": "system", "content": identity})
        messages.append({"role": "system", "content": self._system_prompt})
        messages.append({"role": "system", "content": self._soul})

        # Bootstrap files from workspace (AGENTS.md, SOUL.md, USER.md)
        bootstrap = self._build_bootstrap_block()
        if bootstrap:
            messages.append({"role": "system", "content": bootstrap})

        # Tool contract (from template)
        messages.append({"role": "system", "content": self.tool_contract})

        # Memory block
        memory_block = self._build_memory_block()
        if memory_block is not None:
            messages.append({"role": "system", "content": memory_block})

        # Tool definitions (JSON fallback path only)
        tool_defs_text = ""
        if include_tool_instructions and tool_registry is not None:
            tool_defs = tool_registry.list_compact_definitions()
            if tool_defs:
                from claw.llm.protocol import build_protocol_instructions
                tool_defs_text = build_protocol_instructions(tool_defs)
                messages.append({"role": "system", "content": tool_defs_text})

        # Skill index — lightweight, only name + description
        skill_block = self._build_skill_block()
        if skill_block is not None:
            messages.append({"role": "system", "content": skill_block})

        # --- Volatile suffix (changes every turn) ---
        # Session summary / archived context
        effective_summary = session_summary or session.summary.strip()
        summary_block = self._build_summary_block(effective_summary)
        if summary_block is not None:
            messages.append({"role": "system", "content": summary_block})

        # Recent cross-session history
        if include_recent_history and self._history_log is not None:
            history_block = self._build_recent_history_block(session.session_id)
            if history_block:
                messages.append({"role": "system", "content": history_block})

        # --- Conversation messages ---
        # Build runtime context and append to the LAST user message
        runtime_ctx = _build_runtime_context(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            timezone=self._timezone,
            supplemental_lines=supplemental_runtime_lines,
        )

        unconsolidated = session.get_unconsolidated_messages()
        for i, msg in enumerate(unconsolidated):
            msg_dict = msg.to_dict()
            # Append runtime context to the last user message
            if (
                msg.role == "user"
                and i == len(unconsolidated) - 1
                and runtime_ctx
            ):
                msg_dict["content"] = f"{msg.content}\n\n{runtime_ctx}"
            messages.append(msg_dict)

        if return_budget:
            budget = ContextBudget.measure(
                max_tokens=max_context_tokens or 25600,
                system_prompt=self._system_prompt,
                soul=self._soul,
                memory_block=memory_block,
                tool_defs_text=tool_defs_text,
                skill_block=skill_block,
                summary_block=summary_block,
                messages=unconsolidated,
            )
            return messages, budget

        return messages

    def get_tool_definitions(self, tool_registry=None) -> list[dict[str, Any]]:
        """Return OpenAI-format tool definitions for the API ``tools`` param."""
        if tool_registry is None:
            return []
        return tool_registry.list_definitions()

    def update_system_prompt(self, content: str) -> None:
        """Hot-reload the system prompt without restarting the server."""
        self._system_prompt = content

    def update_soul(self, content: str) -> None:
        """Hot-reload the soul without restarting the server."""
        self._soul = content

    def build_skill_injection_message(self, skill_name: str, user_task: str) -> str:
        """Build a user-message that injects a skill's full content."""
        if self._skill_registry is None:
            return user_task

        try:
            full = self._skill_registry.format_full_content(skill_name)
        except Exception:
            return user_task

        return (
            f"[系统提示] 用户通过 skill 系统使用了 \"{skill_name}\" skill。"
            f"以下是该 skill 的完整说明，请严格按说明执行任务。\n\n"
            f"{full}\n\n"
            f"--- 用户任务 ---\n"
            f"{user_task}"
        )

    # -- internal ------------------------------------------------------------

    def _build_identity_block(self) -> str:
        """Build the identity/runtime preamble using the prompt template system."""
        try:
            from claw.prompts import build_identity
            return build_identity(
                workspace_path=self._workspace_path or "",
                channel=self._channel or "",
                timezone=self._timezone,
            )
        except Exception:
            pass

        # Fallback
        import platform
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        ws = self._workspace_path or "."

        lines = [
            "你是一个 AI 助手（claw），运行在本地工作区内。",
            f"工作区路径: {ws}",
            f"运行环境: {runtime}",
            "",
            "始终以中文回复用户，除非用户明确要求其他语言。",
            "当不确定时，先使用工具获取信息，再回答。不要猜测。",
        ]
        return "\n".join(lines)

    def _build_bootstrap_block(self) -> str | None:
        """Load bootstrap files (AGENTS.md, etc.) from the workspace."""
        if not self._workspace_path:
            return None
        from pathlib import Path
        root = Path(self._workspace_path)
        parts: list[str] = []
        for filename in _BOOTSTRAP_FILES:
            file_path = root / filename
            if file_path.exists():
                try:
                    content = file_path.read_text(encoding="utf-8")
                    parts.append(f"## {filename}\n\n{content}")
                except OSError:
                    pass
        return "\n\n".join(parts) if parts else None

    def _build_memory_block(self) -> str | None:
        """Build a lightweight memory index block.

        Does NOT dump full memory contents into context.
        Instead, provides a count summary and tells the model to use the
        ``recall`` tool when it needs specific information.
        """
        entries = self._memory_store.list()
        if not entries:
            return None

        from claw.memory.store import MEMORY_CATEGORIES

        _LABELS: dict[str, str] = {
            "user_preference": "用户偏好",
            "project": "项目信息",
            "decision": "决策记录",
            "fact": "一般事实",
            "general": "其他",
        }
        category_counts: dict[str, int] = {}
        for e in entries:
            cat = e.category
            category_counts[cat] = category_counts.get(cat, 0) + 1

        parts = [
            f"{_LABELS.get(cat, cat)} {count} 条"
            for cat, count in sorted(category_counts.items())
        ]

        # Include a brief summary of the most recently updated memories
        recent_preview = ""
        sorted_by_time = sorted(
            entries, key=lambda e: e.updated_at, reverse=True
        )[:5]
        if sorted_by_time:
            preview_lines = "\n".join(
                f"- [{e.category}] {e.content[:120]}{'...' if len(e.content) > 120 else ''}"
                for e in sorted_by_time
            )
            recent_preview = f"\n\n最近更新的记忆：\n{preview_lines}"

        return (
            "## 长期记忆 (Memory)\n\n"
            f"当前存储了 {len(entries)} 条长期记忆：{', '.join(parts)}。"
            f"{recent_preview}\n\n"
            "**重要规则**：\n"
            "- 当用户询问关于他自己的任何问题（身份、背景、项目、偏好、"
            "之前的决定等）时，你**必须**先调用 `recall` 工具检索相关记忆，"
            "再基于检索结果回答。不要仅凭当前对话猜测。\n"
            "- 当你在对话中发现了值得长期保留的新信息时，"
            "请使用 `remember` 工具主动保存。\n"
            "- 不要编造或猜测记忆中没有的信息——如果在 recall 中找不到，"
            "诚实地告诉用户你不知道，并建议用户告诉你。"
        )

    @staticmethod
    def _build_summary_block(summary: str) -> str | None:
        if not summary:
            return None
        return (
            "## 会话历史摘要\n\n"
            "以下是当前 session 较早对话的摘要，"
            "这部分历史已经被压缩、不再以原始消息形式保留，"
            "请在回答时结合它一起考虑：\n\n" + summary
        )

    def _build_recent_history_block(self, current_session_id: str) -> str | None:
        """Build a recent-history block from the cross-session history log."""
        if self._history_log is None:
            return None

        entries = self._history_log.read_recent_for_prompt(
            max_entries=_MAX_RECENT_HISTORY,
            exclude_session_id=current_session_id,
        )
        if not entries:
            return None

        # Build text and cap by token budget
        lines = [
            f"- [{e.timestamp}] {e.content[:300]}"
            for e in entries
        ]
        history_text = "\n".join(lines)
        tokens = count_tokens(history_text)
        if tokens > _MAX_HISTORY_TOKENS:
            # Truncate from the front to stay within budget
            ratio = _MAX_HISTORY_TOKENS / tokens
            keep_count = max(1, int(len(entries) * ratio))
            entries = entries[-keep_count:]
            lines = [
                f"- [{e.timestamp}] {e.content[:300]}"
                for e in entries
            ]
            history_text = "\n".join(lines)

        return (
            "## 最近跨会话历史\n\n"
            "以下是其他会话中最近的活动摘要（仅供背景参考）：\n\n"
            + history_text
        )

    def _build_skill_block(self) -> str | None:
        """Build a progressive-loading skill summary for the LLM context.

        Only ``name``, ``description``, and ``path`` are included —
        full instructions are loaded on demand via ``read_file``.

        Always-on skills (``always: true``) are loaded in full.
        Unavailable skills show their missing dependencies.
        """
        if self._skill_registry is None:
            return None

        index = self._skill_registry.list_index(filter_unavailable=False)
        if not index:
            return None

        # Progressive loading summary
        summary = self._skill_registry.build_skills_summary()
        if not summary:
            return None

        lines = [
            "## 可用 Skills",
            "",
            "以下 skills 可扩展你的能力。使用 `read_file` 工具读取 SKILL.md 来加载完整说明。",
            "不可用的 skills 需要先安装依赖。",
            "",
            summary,
        ]

        # Always-on skills: inject full instructions
        always_names = self._skill_registry.get_always_skills()
        if always_names:
            always_parts = []
            for name in always_names:
                try:
                    full = self._skill_registry.format_full_content(name)
                    always_parts.append(full)
                except Exception:
                    pass
            if always_parts:
                lines.append("")
                lines.append("## 自动加载的 Skills（始终可用）")
                lines.append("")
                lines.extend(always_parts)

        lines.append("")
        lines.append(
            "如果你判断当前用户的任务适合使用以上某个 skill，"
            "请调用 `use_skill` 工具，指定 skill 名称和选择理由。"
            "系统会在用户确认后加载该 skill 的完整说明。"
        )
        return "\n".join(lines)

    # -- runtime context helpers (used by session persistence) ----------------

    @staticmethod
    def runtime_context_tag() -> str:
        return _RUNTIME_CONTEXT_TAG

    @staticmethod
    def runtime_context_prefix() -> str:
        return _RUNTIME_CONTEXT_PREFIX
