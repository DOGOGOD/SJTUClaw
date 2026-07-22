"""Parsing and dispatch for claw's internal CLI commands.

``/session ...``, ``/memory ...``, ``/compact``, ``/workspace ...``,
``/approve``, ``/reject`` commands are intercepted here and are never
forwarded to the LLM as ordinary chat messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from zoneinfo import ZoneInfo

from claw.approval.manager import ApprovalManager
from claw.context.compaction import (
    KEEP_RECENT_MESSAGES_MIN,
    CompactionError,
    compact_and_persist,
)
from claw.llm.client import LLMClient
from claw.memory.store import MemoryStore, MemoryStoreError
from claw.session.store import SessionNotFoundError, SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry
from claw.utils import default_timezone_name
from claw.workspace.manager import WorkspaceManager, WorkspaceError
from claw.workspace.rollback import RollbackError, WorkspaceRollbackManager

_COMMAND_PREFIXES = (
    "/session", "/memory", "/compact", "/workspace", "/approve", "/reject",
    "/approvals", "/skill", "/reflect", "/cron", "/help", "/auto",
    "/unlimited", "/pet", "/rollback", "/stop", "/exit",
)

_HELP_TEXT = (
    "SJTUClaw 可用指令：\n\n"
    "  /help                    显示此帮助信息\n"
    "  /session <sub> ...       会话管理\n"
    "    new                      创建新会话\n"
    "    list                     列出所有会话\n"
    "    switch <id>              切换到指定会话\n"
    "    rename <id> <title>      重命名会话\n"
    "    delete <id>              删除会话\n"
    "  /memory <sub> ...        长期记忆管理\n"
    "    add [--category <c>] [--tags <t>] [--importance <1-5>] <内容>\n"
    "    list [--category <类别>] 列出记忆\n"
    "    search <关键词>          搜索记忆\n"
    "    update <id> <新内容>     更新记忆\n"
    "    delete <id>              删除记忆\n"
    "    stats                    记忆统计\n"
    "  /reflect <sub> ...       每日记忆反思\n"
    "    status                   查看反思状态\n"
    "    enable / disable         启用/禁用\n"
    "    time <HH:MM>             设置执行时间\n"
    "    now                      立即执行\n"
    "  /compact                 手动压缩当前会话历史\n"
    "  /workspace <sub> ...     工作区路径管理\n"
    "    set <路径>               设置工作区路径\n"
    "    show                     查看当前工作区\n"
    "    unset                    取消工作区设置\n"
    "  Workspace 回退（需先设置 workspace）：\n"
    "  /rollback                 回退到上一条用户消息发送前\n"
    "  /rollback <n>             回退到倒数第 n 个用户回合之前\n"
    "  /rollback <checkpointId>  回退到指定检查点\n"
    "  /rollback list            列出当前分支的可用回退点\n"
    "  /rollback status          查看回退状态\n"
    "  /rollback undo            撤销最近一次回退（开始新回合后失效）\n"
    "  /approvals               查看待审批操作\n"
    "  /approve [approvalId]    批准操作\n"
    "  /reject [approvalId]     拒绝操作\n"
    "  /skill <sub> ...         Skill 管理\n"
    "    list                     列出可用 Skills\n"
    "    show <name>              查看 Skill 详情\n"
    "    usage                    查看使用记录\n"
    "    <name> <任务描述>        使用指定 Skill 执行任务\n"
    "  /cron <sub> ...          定时作业管理\n"
    "    list                     列出所有作业\n"
    "    status                   服务状态\n"
    "    disable <jobId>          禁用作业\n"
    "    enable <jobId>           启用作业\n"
    "    delete <jobId>           删除作业\n"
    "  /pet <sub> ...           桌面宠物管理\n"
    "    status                   查看运行状态和当前角色\n"
    "    list                     列出可用宠物\n"
    "    open / close             开启或关闭宠物\n"
    "    select <petId>           选择宠物角色\n"
    "    autostart <on|off>       设置是否随 Gateway 启动\n"
    "  /auto                    查看 AUTO 状态和可用指令\n"
    "    status                  查看当前状态\n"
    "    on / off                开启 / 关闭 AUTO 模式\n"
    "    toggle                  切换 AUTO 模式\n"
    "  /unlimited               查看 UNLIMITED 状态和可用指令\n"
    "    status                   查看当前状态\n"
    "    on / off                 开启 / 关闭 UNLIMITED 模式\n"
    "    toggle                   切换 UNLIMITED 模式\n"
    "  /stop                    终止当前正在运行的 Agent 任务\n"
    "  /exit                    退出当前会话\n"
)

_HELP_MARKDOWN = """# SJTUClaw 可用指令

## 基础操作

- `/help`：显示此帮助信息
- `/compact`：手动压缩当前会话历史
- `/stop`：终止当前正在运行的 Agent 任务
- `/exit`：退出当前会话

## 会话管理

- `/session new`：创建新会话
- `/session list`：列出所有会话
- `/session switch <id>`：切换到指定会话
- `/session rename <id> <title>`：重命名会话
- `/session delete <id>`：删除会话

## 长期记忆与反思

- `/memory add [--category <c>] [--tags <t>] [--importance <1-5>] <内容>`：添加记忆
- `/memory list [--category <类别>]`：列出记忆
- `/memory search <关键词>`：搜索记忆
- `/memory update <id> <新内容>`：更新记忆
- `/memory delete <id>`：删除记忆
- `/memory stats`：查看记忆统计
- `/reflect status`：查看每日记忆反思状态
- `/reflect enable` / `/reflect disable`：启用或禁用反思
- `/reflect time <HH:MM>`：设置执行时间
- `/reflect now`：立即执行

## Workspace

- `/workspace set <路径>`：设置工作区路径
- `/workspace show`：查看当前工作区
- `/workspace unset`：取消工作区设置

## Workspace 回退

> 设置 workspace 后自动启用；未设置 workspace 时不支持回退。

- `/rollback`：回退到上一条用户消息发送前
- `/rollback <n>`：回退到倒数第 n 个用户回合之前
- `/rollback <checkpointId>`：回退到指定检查点
- `/rollback list`：列出当前分支的可用回退点
- `/rollback status`：查看回退状态
- `/rollback undo`：撤销最近一次回退；开始新用户回合后 undo 失效

## 审批

- `/approvals`：查看待审批操作
- `/approve [approvalId]`：批准操作
- `/reject [approvalId] [原因]`：拒绝操作

## Agent 模式

- `/auto`：查看 AUTO 状态和可用指令
  - `/auto status`：查看当前状态
  - `/auto on` / `/auto off`：开启或关闭 AUTO 模式
  - `/auto toggle`：切换 AUTO 模式
- `/unlimited`：查看 UNLIMITED 状态和可用指令
  - `/unlimited status`：查看当前状态
  - `/unlimited on` / `/unlimited off`：开启或关闭 UNLIMITED 模式
  - `/unlimited toggle`：切换 UNLIMITED 模式

> **安全提示：** AUTO 模式只会自动批准 workspace 内的操作。UNLIMITED 模式下涉及 workspace 外部的写入、覆盖、删除和 Shell 操作仍需用户明确审批。

## Skill 管理

- `/skill list`：列出可用 Skills
- `/skill show <name>`：查看 Skill 详情
- `/skill usage`：查看使用记录
- `/skill <name> <任务描述>`：使用指定 Skill 执行任务

## 定时作业

- `/cron list`：列出所有作业
- `/cron status`：查看服务状态
- `/cron disable <jobId>`：禁用作业
- `/cron enable <jobId>`：启用作业
- `/cron delete <jobId>`：删除作业

## 桌面宠物

- `/pet` 或 `/pet status`：查看宠物状态
- `/pet list`：列出可用宠物
- `/pet open` / `/pet close`：开启或关闭宠物
- `/pet select <petId>`：选择宠物角色
- `/pet autostart on` / `/pet autostart off`：设置是否随 Gateway 启动
"""


@dataclass
class RuntimeState:
    """Mutable CLI-level state shared across command handlers."""

    session_store: SessionStore
    memory_store: MemoryStore
    llm_client: LLMClient
    current_session_id: str
    workspace_manager: WorkspaceManager | None = None
    approval_manager: ApprovalManager | None = None
    tool_registry: ToolRegistry | None = None
    skill_registry: object | None = None
    reflection_manager: object | None = None
    compaction_worker: object | None = None
    llm_config: object | None = None
    history_log: object | None = None
    cron_service: object | None = None
    pet_catalog: object | None = None
    pet_process: object | None = None
    rollback_manager: WorkspaceRollbackManager | None = None
    # Track the current pending approval for the active agent turn
    pending_approval_id: str | None = None
    # AUTO mode — skip approval for write/shell tools
    auto_mode: bool = False
    # Optional callbacks for gateway integration
    stop_handler: Callable[[], str] | None = None  # () -> result text
    exit_handler: Callable[[], str] | None = None  # () -> result text


def is_command(user_input: str) -> bool:
    """Return True if `user_input` is a slash command."""
    return any(
        user_input == prefix or user_input.startswith(prefix + " ")
        for prefix in _COMMAND_PREFIXES
    )


def handle_command(user_input: str, state: RuntimeState, *, markdown: bool = False) -> str:
    """Handle a command and return the text to print."""
    root, *args = user_input.split()

    def finish(result: str) -> str:
        return _format_command_markdown(result) if markdown else result

    if root == "/session":
        return finish(_handle_session_command(args, state))
    if root == "/memory":
        return finish(_handle_memory_command(user_input, args, state))
    if root == "/compact":
        return finish(_handle_compact_command(state))
    if root == "/workspace":
        return finish(_handle_workspace_command(args, state))
    if root == "/rollback":
        return finish(_handle_rollback_command(args, state))
    if root == "/approvals":
        return finish(_handle_approvals_list(state))
    if root == "/approve":
        return finish(_handle_approve(args, state))
    if root == "/reject":
        return finish(_handle_reject(user_input, args, state))
    if root == "/skill":
        return finish(_handle_skill_command(user_input, args, state))
    if root == "/reflect":
        return finish(_handle_reflect_command(args, state))
    if root == "/cron":
        return finish(_handle_cron_command(args, state))
    if root == "/pet":
        return finish(_handle_pet_command(args, state))
    if root == "/auto":
        return finish(_handle_auto_command(args, state, markdown=markdown))
    if root == "/unlimited":
        return finish(_handle_unlimited_command(args, state, markdown=markdown))
    if root == "/help":
        return _HELP_MARKDOWN if markdown else _HELP_TEXT
    if root == "/stop":
        return finish(_handle_stop_command(state))
    if root == "/exit":
        return finish(_handle_exit_command(state))
    return finish(f"未知命令: {root}（输入 /help 查看可用指令）")


def _format_command_markdown(result: str) -> str:
    """Turn terminal-oriented command output into readable WebUI Markdown."""
    if not result or result.startswith("__SKILL_INVOKE__"):
        return result
    if result.lstrip().startswith(("# ", "## ", "### ", "> ")):
        return result

    lines = result.splitlines()
    formatted: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            formatted.append("")
            continue
        if stripped.startswith("用法:") or stripped.startswith("用法："):
            usage = stripped.split(":", 1)[-1].strip() if ":" in stripped else stripped.split("：", 1)[-1].strip()
            formatted.append(f"**用法：** `{usage}`")
            continue
        if index == 0 and len(lines) > 1 and stripped.endswith((":", "：")):
            formatted.append(f"### {stripped[:-1]}")
            formatted.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if indent >= 4:
            formatted.append(f"  - {stripped}")
        elif indent >= 1:
            formatted.append(f"- {stripped}")
        else:
            formatted.append(line)
    return "\n".join(formatted)


# -- /session ---------------------------------------------------------------


def _handle_session_command(args: list[str], state: RuntimeState) -> str:
    if not args:
        return "用法: /session <new|list|switch|rename|delete> ..."

    sub, rest = args[0], args[1:]

    if sub == "new":
        session = state.session_store.create_session()
        state.current_session_id = session.session_id
        return f"Created session: {session.session_id}"

    if sub == "list":
        return _format_session_list(state)

    if sub == "switch":
        if not rest:
            return "用法: /session switch <sessionId>"
        return _switch_session(rest[0], state)

    if sub == "rename":
        if len(rest) < 2:
            return "用法: /session rename <sessionId> <title>"
        session_id, title = rest[0], " ".join(rest[1:])
        try:
            state.session_store.rename(session_id, title)
        except (SessionNotFoundError, SessionStoreError) as exc:
            return f"[错误] {exc}"
        return f"Renamed session {session_id} to: {title}"

    if sub == "delete":
        if not rest:
            return "用法: /session delete <sessionId>"
        return _delete_session(rest[0], state)

    return f"未知 /session 子命令: {sub}"


def _switch_session(session_id: str, state: RuntimeState) -> str:
    try:
        state.session_store.get(session_id)
    except SessionNotFoundError as exc:
        return f"[错误] {exc}"
    state.current_session_id = session_id
    return f"Switched to session: {session_id}"


def _delete_session(session_id: str, state: RuntimeState) -> str:
    guard = (
        state.rollback_manager.session_guard(session_id)
        if state.rollback_manager is not None else nullcontext()
    )
    try:
        with guard:
            if state.rollback_manager is not None:
                state.rollback_manager.disable(session_id)
            if state.workspace_manager is not None:
                state.workspace_manager.unset(session_id)
                state.workspace_manager.set_unlimited(session_id, False)
            state.session_store.delete(session_id)
    except (SessionNotFoundError, SessionStoreError, OSError, RollbackError) as exc:
        return f"[错误] {exc}"

    if state.current_session_id != session_id:
        return f"Deleted session: {session_id}"

    summaries = state.session_store.list_summaries()
    if summaries:
        fallback_id = summaries[0].session_id
    else:
        fallback_id = state.session_store.ensure_default_session().session_id
    state.current_session_id = fallback_id
    return (
        f"Deleted session: {session_id}\n"
        f"当前 session 已被删除，已自动切换到: {fallback_id}"
    )


def _format_session_list(state: RuntimeState) -> str:
    summaries = state.session_store.list_summaries()
    if not summaries:
        return "Sessions: (empty)"

    lines = ["Sessions:"]
    for summary in summaries:
        marker = "*" if summary.session_id == state.current_session_id else " "
        lines.append(
            f"{marker} {summary.session_id}\t{summary.title}\t"
            f"messages={summary.message_count}\tupdated={summary.updated_at}"
        )
    return "\n".join(lines)


# -- /memory ------------------------------------------------------------


def _handle_memory_command(raw_input: str, args: list[str], state: RuntimeState) -> str:
    if not args:
        return "用法: /memory <add|list|search|update|delete|stats> ..."

    sub = args[0]

    if sub == "add":
        return _add_memory(raw_input, state)

    if sub == "list":
        return _format_memory_list(args[1:], state)

    if sub == "search":
        if len(args) < 2:
            return "用法: /memory search <关键词>"
        return _search_memory(" ".join(args[1:]), state)

    if sub == "stats":
        return _memory_stats(state)

    if sub == "update":
        if len(args) < 3:
            return "用法: /memory update <memoryId> <新内容>"
        memory_id = args[1]
        new_content = " ".join(args[2:])
        return _update_memory(memory_id, new_content, state)

    if sub == "delete":
        if len(args) < 2:
            return "用法: /memory delete <memoryId>"
        return _delete_memory(args[1], state)

    return f"未知 /memory 子命令: {sub}"


def _add_memory(raw_input: str, state: RuntimeState) -> str:
    """Parse and add a memory entry.

    Supports two forms:
        /memory add <content>                          # legacy
        /memory add --category <c> --tags <t1,t2> <content>  # structured
    """
    prefix = "/memory add "
    if not raw_input.startswith(prefix):
        return "用法: /memory add [--category <类别>] [--tags <t1,t2>] [--importance <1-5>] <内容>"
    rest = raw_input[len(prefix):]

    # Parse optional flags
    category = "general"
    tags: list[str] = []
    importance = 3

    # --category <cat>
    import re
    cat_match = re.match(r"^--category\s+(\S+)", rest)
    if cat_match:
        category = cat_match.group(1)
        rest = rest[cat_match.end():]

    # --tags <t1,t2,...>
    tags_match = re.match(r"^--tags\s+(\S+)", rest)
    if tags_match:
        tags = [t.strip() for t in tags_match.group(1).split(",") if t.strip()]
        rest = rest[tags_match.end():]

    # --importance <1-5>
    imp_match = re.match(r"^--importance\s+(\d)", rest)
    if imp_match:
        try:
            importance = int(imp_match.group(1))
        except ValueError:
            pass
        rest = rest[imp_match.end():]

    content = rest.strip()
    if not content:
        return "用法: /memory add [--category <类别>] [--tags <t1,t2>] [--importance <1-5>] <内容>"

    try:
        entry = state.memory_store.add(
            content=content,
            category=category,
            tags=tags,
            importance=importance,
        )
    except MemoryStoreError as exc:
        return f"[错误] {exc}"

    tag_str = f" [tags: {', '.join(entry.tags)}]" if entry.tags else ""
    return f"Added memory: {entry.memory_id} [{entry.category}]{tag_str}"


def _format_memory_list(extra_args: list[str], state: RuntimeState) -> str:
    """List memory entries, optionally filtered by category."""
    category: str | None = None
    if len(extra_args) >= 2 and extra_args[0] == "--category":
        category = extra_args[1]

    from claw.memory.store import MEMORY_CATEGORIES
    _LABELS: dict[str, str] = {
        "user_preference": "pref",
        "project": "proj",
        "decision": "decn",
        "fact": "fact",
        "general": "gen",
    }

    try:
        entries = state.memory_store.list_by_category(category)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"

    if not entries:
        filter_text = f" (category={category})" if category else ""
        return f"Memory{filter_text}: (empty)"

    filter_text = f" (category={category})" if category else ""
    lines = [f"Memory{filter_text}:"]
    for entry in entries:
        cat_short = _LABELS.get(entry.category, entry.category[:4])
        tag_str = f" [tags: {', '.join(entry.tags)}]" if entry.tags else ""
        imp_str = f" ★{entry.importance}" if entry.importance != 3 else ""
        content_preview = entry.content[:80] + ("..." if len(entry.content) > 80 else "")
        lines.append(
            f"  {entry.memory_id} [{cat_short}{imp_str}] {content_preview}{tag_str}"
        )
    return "\n".join(lines)


def _search_memory(query: str, state: RuntimeState) -> str:
    """Search memory entries by keyword."""
    try:
        results = state.memory_store.recall(query=query, limit=10)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"

    if not results:
        return f"未找到与 \"{query}\" 相关的记忆。"

    lines = [f"搜索 \"{query}\" 的结果 ({len(results)} 条):"]
    for i, entry in enumerate(results, 1):
        tag_str = f" [tags: {', '.join(entry.tags)}]" if entry.tags else ""
        lines.append(f"  [{i}] {entry.memory_id} [{entry.category}] {entry.content}{tag_str}")
    return "\n".join(lines)


def _memory_stats(state: RuntimeState) -> str:
    """Show memory statistics by category."""
    stats = state.memory_store.stats()
    total = sum(stats.values())
    if total == 0:
        return "Memory 统计: (empty)"
    lines = ["Memory 统计:", f"  总条目: {total} 条"]
    for label, count in sorted(stats.items()):
        lines.append(f"  {label}: {count} 条")
    return "\n".join(lines)


def _update_memory(memory_id: str, new_content: str, state: RuntimeState) -> str:
    """Update a memory entry's content."""
    try:
        state.memory_store.update(memory_id, new_content)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"
    return f"Updated memory: {memory_id}"


def _delete_memory(memory_id: str, state: RuntimeState) -> str:
    try:
        state.memory_store.delete(memory_id)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"
    return f"Deleted memory: {memory_id}"


# -- /compact -----------------------------------------------------------


def _handle_compact_command(state: RuntimeState) -> str:
    session = state.session_store.get(state.current_session_id)

    if len(session.messages) <= KEEP_RECENT_MESSAGES_MIN:
        return (
            f"当前 session 只有 {len(session.messages)} 条消息，"
            f"不超过保留窗口（{KEEP_RECENT_MESSAGES_MIN}），无需压缩。"
        )

    try:
        outcome = compact_and_persist(session, state.session_store, state.llm_client)
    except CompactionError as exc:
        return f"[错误] {exc}"

    result = outcome.result
    lines = [
        f"Compacted session {session.session_id}.",
        f"Old messages: {result.old_message_count}",
        f"Recent messages: {result.recent_message_count}",
        "Summary updated: yes",
        "Summary:",
        result.summary,
    ]
    if outcome.save_error:
        lines.append(f"[警告] 压缩结果保存可能未成功: {outcome.save_error}")
    return "\n".join(lines)


# -- /workspace (Step 8) -------------------------------------------------


def _handle_workspace_command(args: list[str], state: RuntimeState) -> str:
    if state.workspace_manager is None:
        return "Workspace manager 未初始化。"

    if not args:
        return "用法: /workspace <set|show|unset> ..."

    sub = args[0]
    sid = state.current_session_id

    if sub == "set":
        if len(args) < 2:
            return "用法: /workspace set <路径>"
        path_str = " ".join(args[1:])
        previous = state.workspace_manager.get(sid)
        guard = (
            state.rollback_manager.session_guard(sid)
            if state.rollback_manager is not None else nullcontext()
        )
        try:
            with guard:
                resolved = state.workspace_manager.set(sid, path_str)
                if state.rollback_manager is not None:
                    state.rollback_manager.enable(sid, state.session_store.get(sid))
            return f"Workspace 已设置为: {resolved}"
        except (WorkspaceError, RollbackError, OSError) as exc:
            try:
                if previous is None:
                    state.workspace_manager.unset(sid)
                else:
                    state.workspace_manager.set(sid, str(previous))
            except (WorkspaceError, OSError):
                pass
            return f"[错误] {exc}"

    if sub == "show":
        ws = state.workspace_manager.get(sid)
        if ws is None:
            return "当前 session 未设置 workspace。使用 /workspace set <路径> 来设置。"
        return f"当前 workspace: {ws}"

    if sub == "unset":
        guard = (
            state.rollback_manager.session_guard(sid)
            if state.rollback_manager is not None else nullcontext()
        )
        with guard:
            if state.rollback_manager is not None:
                state.rollback_manager.disable(sid)
            state.workspace_manager.unset(sid)
        return "Workspace 已取消设置。"

    return f"未知 /workspace 子命令: {sub}"


def _handle_rollback_command(args: list[str], state: RuntimeState) -> str:
    manager = state.rollback_manager
    if manager is None:
        return "Workspace 回退服务未初始化。"
    sid = state.current_session_id
    sub = args[0].lower() if args else "1"
    try:
        if sub in ("status", "show"):
            status = manager.status(sid)
            if not status["enabled"]:
                return "当前 session 未启用回退。请先设置 workspace。"
            return (
                f"Workspace 回退已启用。\n"
                f"路径: {status['workspace']}\n"
                f"回退点: {status['checkpointCount']}\n"
                f"可撤销回退: {'是' if status['undoAvailable'] else '否'}"
            )
        if sub == "list":
            checkpoints = [item for item in manager.list_checkpoints(sid) if item["kind"] == "turn"]
            if not checkpoints:
                return "当前没有可用的消息回退点。"
            lines = ["可用回退点："]
            for index, item in enumerate(checkpoints, 1):
                warning = " [仅 workspace]" if item["partial"] else ""
                lines.append(
                    f"  {index}. {item['checkpointId']}  {item['messagePreview']}{warning}"
                )
            return "\n".join(lines)
        if sub == "undo":
            result = manager.undo(sid)
            return (
                f"已撤销上一次回退。恢复 {result['restored']} 个路径，"
                f"删除 {result['deleted']} 个路径。"
            )
        target: str | int | None
        target = int(sub) if sub.isdigit() else sub
        result = manager.rollback(sid, target)
        warning = "\n注意：UNLIMITED 模式下 workspace 外的改动未恢复。" if result["partial"] else ""
        return (
            f"回退完成。恢复 {result['restored']} 个路径，"
            f"删除 {result['deleted']} 个路径。可使用 /rollback undo 撤销。{warning}"
        )
    except (RollbackError, WorkspaceError, OSError) as exc:
        return f"[错误] {exc}"


# -- /approvals (list pending) ---------------------------------------------


def _handle_approvals_list(state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    pending = [r for r in state.approval_manager.get_pending()
               if r.session_id == state.current_session_id]
    if not pending:
        return "当前没有待审批的操作。"

    lines = ["待审批操作:"]
    for r in pending:
        lines.append(
            f"  [{r.approval_id}] {r.tool_name} "
            f"session={r.session_id}"
        )
        # Show key args
        args_safe = {
            k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v)
            for k, v in r.tool_args.items()
        }
        lines.append(f"    参数: {args_safe}")
    lines.append("\n使用 /approve <approvalId> 批准，/reject <approvalId> [原因] 拒绝。")
    return "\n".join(lines)


# -- /approve --------------------------------------------------------------


def _handle_approve(args: list[str], state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    if not args:
        # If there's exactly one pending in this session, use it.
        pending = [r for r in state.approval_manager.get_pending()
                   if r.session_id == state.current_session_id]
        if len(pending) == 1:
            approval_id = pending[0].approval_id
        else:
            return "用法: /approve <approvalId>（或当只有一个待审批时省略 ID）"
    else:
        approval_id = args[0]

    existing = state.approval_manager.get(approval_id)
    if existing is None or existing.session_id != state.current_session_id:
        return f"未找到当前会话的审批请求: {approval_id}"
    req = state.approval_manager.approve(approval_id)
    return f"已批准: [{req.approval_id}] {req.tool_name}"


# -- /reject ---------------------------------------------------------------


def _handle_reject(raw_input: str, args: list[str], state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    if not args:
        pending = [r for r in state.approval_manager.get_pending()
                   if r.session_id == state.current_session_id]
        if len(pending) == 1:
            approval_id = pending[0].approval_id
        else:
            return "用法: /reject <approvalId> [原因]（或当只有一个待审批时省略 ID）"
    else:
        approval_id = args[0]

    # Extract reason: everything after the approval ID
    if len(args) > 1:
        reason = " ".join(args[1:])
    elif len(args) == 1:
        reason = ""
    else:
        reason = ""

    existing = state.approval_manager.get(approval_id)
    if existing is None or existing.session_id != state.current_session_id:
        return f"未找到当前会话的审批请求: {approval_id}"
    req = state.approval_manager.reject(approval_id, reason)
    reason_text = f"原因: {reason}" if reason else "未提供原因"
    return f"已拒绝: [{req.approval_id}] {req.tool_name} ({reason_text})"


# -- /skill (Step 9) -------------------------------------------------------


def _handle_skill_command(
    raw_input: str, args: list[str], state: RuntimeState
) -> str:
    """Handle /skill commands.  Returns either a plain result string or
    a special ``__SKILL_INVOKE__`` sentinel indicating that the caller
    should run an agent turn with the skill content pre-loaded.
    """
    if state.skill_registry is None:
        return "Skill registry 未初始化。"

    if not args:
        return "用法: /skill <list|show|usage|<skill-name> <task>> ..."

    sub = args[0]

    # /skill list
    if sub == "list":
        skills = state.skill_registry.list_skills()
        if not skills:
            return "Skills: (empty)"
        lines = ["Skills:"]
        for s in skills:
            lines.append(f"  {s.name}")
            lines.append(f"    {s.description}")
        return "\n".join(lines)

    # /skill show <name>
    if sub == "show":
        if len(args) < 2:
            return "用法: /skill show <skill-name>"
        name = args[1]
        skill = state.skill_registry.get_skill(name)
        if skill is None:
            return f"未找到 skill: \"{name}\"。使用 /skill list 查看可用 skill。"
        lines = [
            f"Skill: {skill.name}",
            f"描述: {skill.description}",
            "",
            "使用说明:",
            skill.instructions[:1500],
        ]
        if len(skill.instructions) > 1500:
            lines.append("...(已截断，完整内容在加载时可见)")
        if skill.assets:
            lines.append(f"\n附带资源: {[a.name for a in skill.assets]}")
        if skill.references:
            lines.append(f"\n参考文件: {[r.name for r in skill.references]}")
        return "\n".join(lines)

    # /skill usage
    if sub == "usage":
        session = state.session_store.get(state.current_session_id)
        records = session.skill_usage
        if not records:
            return "当前 session 暂无 skill 使用记录。"
        lines = [f"Skill 使用记录 (session: {state.current_session_id}):"]
        for i, r in enumerate(records, 1):
            source_label = "显式调用" if r.get("source") == "explicit" else "模型自主选择"
            lines.append(
                f"  [{i}] {r.get('skillName', '?')} | {source_label} | "
                f"{r.get('usedAt', '?')}"
            )
            lines.append(f"      任务: {r.get('userTask', '')[:100]}")
            if r.get("source") == "auto" and r.get("autoReason"):
                lines.append(f"      选择理由: {r.get('autoReason', '')}")
            if r.get("outputPath"):
                lines.append(f"      输出路径: {r.get('outputPath', '')}")
        return "\n".join(lines)

    # /skill <skill-name> <task> — explicit invocation
    skill_name = sub
    skill = state.skill_registry.get_skill(skill_name)
    if skill is None:
        return f"未找到 skill: \"{skill_name}\"。使用 /skill list 查看可用 skill。"

    prefix = f"/skill {skill_name} "
    if not raw_input.startswith(prefix):
        return f"用法: /skill {skill_name} <任务描述>"
    task = raw_input[len(prefix):].strip()
    if not task:
        return f"用法: /skill {skill_name} <任务描述>"

    # Return sentinel — the REPL will detect this and call run_agent_turn
    # with the skill pre-loaded.
    return f"__SKILL_INVOKE__|{skill_name}|{task}"


# -- /reflect (daily memory reflection) --------------------------------------


def _handle_reflect_command(args: list[str], state: RuntimeState) -> str:
    """Handle /reflect commands for daily memory reflection config."""
    if state.reflection_manager is None:
        return "Reflection manager 未初始化。"

    mgr = state.reflection_manager

    if not args:
        return "用法: /reflect <status|enable|disable|time|now> ..."

    sub = args[0]

    if sub == "status":
        config = mgr.get_config()
        last_run = config.get("lastRunAt") or "从未"
        history = config.get("runHistory", [])
        last_result = ""
        if history:
            last = history[-1]
            last_result = (
                f"  上次: {last.get('runAt','?')} | "
                f"检查了 {last.get('sessionsReviewed',0)} session | "
                f"提取了 {last.get('factsExtracted',0)} 条记忆 | "
                f"状态: {last.get('status','?')}"
            )
        lines = [
            "📋 每日记忆反思配置:",
            f"  状态: {'✅ 已启用' if config.get('enabled') else '❌ 已禁用'}",
            f"  时间: 每天 {config.get('time', '?')}",
            f"  上次执行: {last_run}",
        ]
        if last_result:
            lines.append(last_result)
        return "\n".join(lines)

    if sub == "enable":
        mgr.update_config(enabled=True)
        return "✅ 每日记忆反思已启用。每天定时自动整理对话，提取长期记忆。"

    if sub == "disable":
        mgr.update_config(enabled=False)
        return "❌ 每日记忆反思已禁用。"

    if sub == "time":
        if len(args) < 2:
            return "用法: /reflect time <HH:MM>（如 /reflect time 23:00）"
        new_time = args[1]
        import re as _re
        if not _re.match(r"^\d{2}:\d{2}$", new_time):
            return "时间格式错误，请使用 HH:MM 格式（如 23:00）"
        mgr.update_config(time=new_time)
        return f"⏰ 每日反思时间已设置为 {new_time}。"

    if sub == "now":
        result = mgr.run_now()
        if result.get("ok"):
            return (
                f"✅ 即时反思完成。\n"
                f"  检查了 {result.get('sessionsReviewed', 0)} 个 session\n"
                f"  提取了 {result.get('factsExtracted', 0)} 条记忆"
            )
        else:
            return f"❌ 反思失败: {result.get('error', '未知错误')}"

    return f"未知 /reflect 子命令: {sub}"


# -- /cron ------------------------------------------------------------------


def _handle_cron_command(args: list[str], state: RuntimeState) -> str:
    """Handle /cron commands."""
    if state.cron_service is None:
        return "Cron 服务未初始化。"

    sub = args[0] if args else "list"

    if sub == "list":
        try:
            jobs = state.cron_service.list_jobs(include_disabled=True)
            if not jobs:
                return "暂无定时作业。使用 cron 工具创建。"
            lines = ["Cron 作业:"]
            for j in jobs:
                kind = j.payload.kind
                protected = " [系统]" if kind == "system_event" else ""
                enabled = "" if j.enabled else " [已禁用]"
                lines.append(
                    f"  {j.id} {j.name}{protected}{enabled} "
                    f"schedule={j.schedule.kind}"
                )
                if j.state.last_status:
                    lines.append(f"    上次: {j.state.last_status}")
                if j.state.next_run_at_ms:
                    from datetime import datetime
                    tz_name = j.schedule.tz or default_timezone_name()
                    dt = datetime.fromtimestamp(
                        j.state.next_run_at_ms / 1000,
                        tz=ZoneInfo(tz_name),
                    )
                    lines.append(f"    下次: {dt.isoformat()} ({tz_name})")
            return "\n".join(lines)
        except Exception as exc:
            return f"错误: {exc}"

    if sub == "status":
        status = state.cron_service.status()
        return (
            f"Cron 服务: {'运行中' if status['enabled'] else '已停止'}\n"
            f"作业数: {status['jobs']}"
        )

    if sub == "disable":
        if len(args) < 2:
            return "用法: /cron disable <jobId>"
        job = state.cron_service.enable_job(args[1], enabled=False)
        if job is None:
            return f"作业不存在: {args[1]}"
        return f"已禁用作业: {args[1]}"

    if sub == "enable":
        if len(args) < 2:
            return "用法: /cron enable <jobId>"
        job = state.cron_service.enable_job(args[1], enabled=True)
        if job is None:
            return f"作业不存在: {args[1]}"
        return f"已启用作业: {args[1]}"

    if sub == "delete":
        if len(args) < 2:
            return "用法: /cron delete <jobId>"
        result = state.cron_service.remove_job(args[1])
        if result == "removed":
            return f"已删除作业: {args[1]}"
        if result == "protected":
            return f"无法删除受保护的系统作业: {args[1]}"
        return f"作业不存在: {args[1]}"

    return f"用法: /cron <list|status|disable|enable|delete> ..."


# -- /pet -------------------------------------------------------------------


def _handle_pet_command(args: list[str], state: RuntimeState) -> str:
    """Inspect and manage the desktop pet from CLI and WebUI."""
    if state.pet_catalog is None or state.pet_process is None:
        return "桌面宠物服务未初始化，请通过 sjtuclaw gateway 使用此功能。"

    catalog = state.pet_catalog
    process = state.pet_process
    sub = args[0].lower() if args else "status"

    if sub in ("status", "show", "settings", "config", "help", "?"):
        settings = catalog.load_settings()
        pet = catalog.get_pet(settings.selected_pet_id)
        name = pet.get("displayName", settings.selected_pet_id) if pet else settings.selected_pet_id
        return (
            "桌面宠物状态：\n"
            f"  窗口: {'正在运行' if process.running else '已关闭'}\n"
            f"  功能: {'已启用' if settings.enabled else '已关闭'}\n"
            f"  当前角色: {name} ({settings.selected_pet_id})\n"
            f"  随 Gateway 启动: {'是' if settings.launch_on_gateway_start else '否'}\n\n"
            "用法: /pet <status|list|open|close|select|autostart>"
        )

    if sub == "list":
        settings = catalog.load_settings()
        pets = catalog.list_pets()
        if not pets:
            return "暂无可用宠物。"
        lines = ["可用宠物："]
        for pet in pets:
            selected = " [当前]" if pet["id"] == settings.selected_pet_id else ""
            lines.append(f"  {pet['id']}  {pet['displayName']}{selected}")
            if pet.get("description"):
                lines.append(f"    {pet['description']}")
        return "\n".join(lines)

    if sub in ("open", "on", "enable"):
        catalog.update_settings(enabled=True)
        started = process.start()
        if process.running:
            return "桌面宠物已开启。"
        return "已提交桌面宠物启动请求。" if started else "桌面宠物已经在运行。"

    if sub in ("close", "off", "disable"):
        catalog.update_settings(enabled=False)
        process.stop()
        return "桌面宠物已关闭。"

    if sub == "select":
        if len(args) < 2:
            return "用法: /pet select <petId>"
        pet_id = args[1]
        try:
            settings = catalog.update_settings(selected_pet_id=pet_id)
        except ValueError as exc:
            return f"选择失败: {exc}"
        if process.running:
            process.stop()
            process.start()
        pet = catalog.get_pet(settings.selected_pet_id)
        name = pet.get("displayName", pet_id) if pet else pet_id
        return f"已选择宠物: {name} ({pet_id})"

    if sub == "autostart":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            return "用法: /pet autostart <on|off>"
        enabled = args[1].lower() == "on"
        catalog.update_settings(launch_on_gateway_start=enabled)
        return f"随 Gateway 启动已{'开启' if enabled else '关闭'}。"

    return "用法: /pet <status|list|open|close|select|autostart>"

# -- /auto ------------------------------------------------------------------


def _handle_auto_command(
    args: list[str], state: RuntimeState, *, markdown: bool = False
) -> str:
    """Inspect or explicitly change AUTO mode."""
    sub = args[0].lower() if args else "status"

    if sub in ("status", "show", "help", "?"):
        state_text = "已开启" if state.auto_mode else "已关闭"
        if markdown:
            return (
                f"## AUTO 模式\n\n"
                f"**当前状态：{state_text}**\n\n"
                "### 可用指令\n\n"
                "- `/auto on`：开启 AUTO 模式\n"
                "- `/auto off`：关闭 AUTO 模式\n"
                "- `/auto toggle`：切换 AUTO 模式\n"
                "- `/auto status`：查看当前状态\n\n"
                "> AUTO 模式会自动批准 workspace 内的写入和 Shell 操作。"
                "UNLIMITED 模式下涉及非 workspace 区域的危险操作仍需用户明确审批。"
            )
        return (
            f"AUTO 模式当前{state_text}。\n\n"
            "可用指令：\n"
            "  /auto on      开启 AUTO 模式\n"
            "  /auto off     关闭 AUTO 模式\n"
            "  /auto toggle  切换 AUTO 模式\n"
            "  /auto status  查看当前状态\n\n"
            "AUTO 模式会自动批准 workspace 内的写入和 Shell 操作；"
            "UNLIMITED 模式下涉及非 workspace 区域的危险操作仍需用户明确审批。"
        )

    if sub in ("on", "enable", "1"):
        state.auto_mode = True
        return "AUTO 模式已开启。Agent 在 workspace 内的写操作和 Shell 命令将自动执行，无需逐一审批。"
    elif sub in ("off", "disable", "0"):
        state.auto_mode = False
        return "AUTO 模式已关闭。Agent 的写操作和 shell 命令恢复审批。"
    elif sub == "toggle":
        state.auto_mode = not state.auto_mode
        state_text = "开启" if state.auto_mode else "关闭"
        return f"AUTO 模式已{state_text}。"

    return (
        f"未知的 AUTO 子指令：{sub}\n"
        "请使用 /auto 查看帮助，或使用 /auto on、/auto off、/auto toggle。"
    )


# -- /unlimited -------------------------------------------------------------


def _handle_unlimited_command(
    args: list[str], state: RuntimeState, *, markdown: bool = False
) -> str:
    """Show or change UNLIMITED mode for the current session."""
    if state.workspace_manager is None:
        return "Workspace manager 未初始化，无法使用此功能。"
    sid = state.current_session_id
    sub = args[0].lower() if args else "help"

    def _help() -> str:
        enabled = state.workspace_manager.is_unlimited(sid)
        status = "已开启" if enabled else "已关闭"
        if markdown:
            return (
                "## UNLIMITED 模式\n\n"
                f"**当前状态：{status}**\n\n"
                "### 可用指令\n\n"
                "- `/unlimited status`：查看当前状态\n"
                "- `/unlimited on`：开启模式，允许访问 workspace 之外的路径\n"
                "- `/unlimited off`：关闭模式，恢复 workspace 边界限制\n"
                "- `/unlimited toggle`：切换当前模式\n\n"
                "> **安全规则：** UNLIMITED 只解除路径限制；写入、覆盖、删除和 "
                "Shell 操作仍必须由用户逐次审批，AUTO 模式不能绕过审批。"
            )
        return (
            f"UNLIMITED 模式当前{status}。\n\n"
            "可用指令：\n"
            "- `/unlimited status`：查看当前状态\n"
            "- `/unlimited on`：开启模式，允许访问 workspace 之外的路径\n"
            "- `/unlimited off`：关闭模式，恢复 workspace 边界限制\n"
            "- `/unlimited toggle`：切换当前模式\n\n"
            "安全规则：UNLIMITED 只解除路径限制；写入、覆盖、删除和 shell 操作"
            "仍必须由用户逐次审批，AUTO 模式不能绕过审批。"
        )

    if sub in ("help", "status", "show"):
        return _help()
    if sub in ("on", "enable", "1"):
        state.workspace_manager.set_unlimited(sid, True)
        return (
            "UNLIMITED 模式已开启。\n"
            "Agent 现在可以读取、写入、删除任意路径的文件，"
            "不受 workspace 限制。写入、删除和 shell 操作仍需逐次审批，"
            "AUTO 模式不会跳过这些审批。\n"
            "输入 /unlimited off 关闭。"
        )
    elif sub in ("off", "disable", "0"):
        state.workspace_manager.set_unlimited(sid, False)
        return "UNLIMITED 模式已关闭。Agent 的操作将恢复到 workspace 限制。"
    elif sub == "toggle":
        unlimited = state.workspace_manager.is_unlimited(sid)
        if unlimited:
            state.workspace_manager.set_unlimited(sid, False)
            return "UNLIMITED 模式已关闭。Agent 的操作将恢复到 workspace 限制。"
        else:
            state.workspace_manager.set_unlimited(sid, True)
            return (
                "UNLIMITED 模式已开启。\n"
                "Agent 现在可以读取、写入、删除任意路径的文件，"
                "不受 workspace 限制。危险操作仍需逐次审批。"
            )
    return f"未知的 UNLIMITED 指令: {sub}\n\n{_help()}"


# -- /stop ------------------------------------------------------------------


def _handle_stop_command(state: RuntimeState) -> str:
    """Stop the currently running agent turn."""
    if state.stop_handler is not None:
        return state.stop_handler()
    return "没有正在运行的任务。"


# -- /exit ------------------------------------------------------------------


def _handle_exit_command(state: RuntimeState) -> str:
    """Exit the current session."""
    if state.exit_handler is not None:
        return state.exit_handler()
    return "再见！"
