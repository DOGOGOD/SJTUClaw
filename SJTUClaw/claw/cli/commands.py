"""Parsing and dispatch for claw's internal CLI commands.

``/session ...``, ``/memory ...``, ``/compact``, ``/workspace ...``,
``/approve``, ``/reject`` commands are intercepted here and are never
forwarded to the LLM as ordinary chat messages.
"""

from __future__ import annotations

from dataclasses import dataclass

from claw.approval.manager import ApprovalManager
from claw.context.compaction import (
    KEEP_RECENT_MESSAGES,
    CompactionError,
    compact_and_persist,
)
from claw.llm.client import LLMClient
from claw.memory.store import MemoryStore, MemoryStoreError
from claw.session.store import SessionNotFoundError, SessionStore, SessionStoreError
from claw.tools.base import ToolRegistry
from claw.workspace.manager import WorkspaceManager, WorkspaceError

_COMMAND_PREFIXES = (
    "/session", "/memory", "/compact", "/workspace", "/approve", "/reject",
    "/approvals", "/skill",
)


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
    # Track the current pending approval for the active agent turn
    pending_approval_id: str | None = None


def is_command(user_input: str) -> bool:
    """Return True if `user_input` is a slash command."""
    return any(
        user_input == prefix or user_input.startswith(prefix + " ")
        for prefix in _COMMAND_PREFIXES
    )


def handle_command(user_input: str, state: RuntimeState) -> str:
    """Handle a command and return the text to print."""
    root, *args = user_input.split()

    if root == "/session":
        return _handle_session_command(args, state)
    if root == "/memory":
        return _handle_memory_command(user_input, args, state)
    if root == "/compact":
        return _handle_compact_command(state)
    if root == "/workspace":
        return _handle_workspace_command(args, state)
    if root == "/approvals":
        return _handle_approvals_list(state)
    if root == "/approve":
        return _handle_approve(args, state)
    if root == "/reject":
        return _handle_reject(user_input, args, state)
    if root == "/skill":
        return _handle_skill_command(user_input, args, state)
    return f"未知命令: {root}"


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
    try:
        state.session_store.delete(session_id)
    except (SessionNotFoundError, SessionStoreError) as exc:
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
        return "用法: /memory <add|list|delete> ..."

    sub = args[0]

    if sub == "add":
        return _add_memory(raw_input, state)

    if sub == "list":
        return _format_memory_list(state)

    if sub == "delete":
        if len(args) < 2:
            return "用法: /memory delete <memoryId>"
        return _delete_memory(args[1], state)

    return f"未知 /memory 子命令: {sub}"


def _add_memory(raw_input: str, state: RuntimeState) -> str:
    prefix = "/memory add "
    if not raw_input.startswith(prefix):
        return "用法: /memory add <content>"
    content = raw_input[len(prefix):].strip()
    try:
        entry = state.memory_store.add(content)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"
    return f"Added memory: {entry.memory_id}"


def _format_memory_list(state: RuntimeState) -> str:
    entries = state.memory_store.list()
    if not entries:
        return "Memory: (empty)"
    lines = ["Memory:"]
    for entry in entries:
        lines.append(f"  {entry.memory_id}\t{entry.content}")
    return "\n".join(lines)


def _delete_memory(memory_id: str, state: RuntimeState) -> str:
    try:
        state.memory_store.delete(memory_id)
    except MemoryStoreError as exc:
        return f"[错误] {exc}"
    return f"Deleted memory: {memory_id}"


# -- /compact -----------------------------------------------------------


def _handle_compact_command(state: RuntimeState) -> str:
    session = state.session_store.get(state.current_session_id)

    if len(session.messages) <= KEEP_RECENT_MESSAGES:
        return (
            f"当前 session 只有 {len(session.messages)} 条消息，"
            f"不超过保留窗口（{KEEP_RECENT_MESSAGES}），无需压缩。"
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
        try:
            resolved = state.workspace_manager.set(sid, path_str)
            return f"Workspace 已设置为: {resolved}"
        except WorkspaceError as exc:
            return f"[错误] {exc}"

    if sub == "show":
        ws = state.workspace_manager.get(sid)
        if ws is None:
            return "当前 session 未设置 workspace。使用 /workspace set <路径> 来设置。"
        return f"当前 workspace: {ws}"

    if sub == "unset":
        state.workspace_manager.unset(sid)
        return "Workspace 已取消设置。"

    return f"未知 /workspace 子命令: {sub}"


# -- /approvals (list pending) ---------------------------------------------


def _handle_approvals_list(state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    pending = state.approval_manager.get_pending()
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
        # If there's exactly one pending, use it
        pending = state.approval_manager.get_pending()
        if len(pending) == 1:
            approval_id = pending[0].approval_id
        else:
            return "用法: /approve <approvalId>（或当只有一个待审批时省略 ID）"
    else:
        approval_id = args[0]

    req = state.approval_manager.approve(approval_id)
    if req is None:
        return f"未找到审批请求: {approval_id}"
    return f"已批准: [{req.approval_id}] {req.tool_name}"


# -- /reject ---------------------------------------------------------------


def _handle_reject(raw_input: str, args: list[str], state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    if not args:
        pending = state.approval_manager.get_pending()
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

    req = state.approval_manager.reject(approval_id, reason)
    if req is None:
        return f"未找到审批请求: {approval_id}"
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
