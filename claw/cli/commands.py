"""Parsing and dispatch for claw's internal CLI commands.

``/session ...``, ``/memory ...``, ``/compact``, ``/workspace ...``,
``/approve``, ``/reject`` commands are intercepted here and are never
forwarded to the LLM as ordinary chat messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from contextlib import nullcontext
from typing import Callable
from zoneinfo import ZoneInfo

from claw.approval.manager import ApprovalManager
from claw.context.compaction import (
    KEEP_RECENT_MESSAGES_MIN,
    CompactionError,
    compact_and_persist,
    format_compaction_brief,
)
from claw.llm.client import LLMClient
from claw.memory.reflection import is_valid_reflection_time
from claw.memory.store import MemoryStore, MemoryStoreError
from claw.sandbox import SandboxError
from claw.session.store import (
    AUTO_MODE_METADATA_KEY,
    SessionNotFoundError,
    SessionStore,
    SessionStoreError,
)
from claw.tools.base import ToolRegistry
from claw.utils import default_timezone_name
from claw.workspace.manager import WorkspaceManager, WorkspaceError
from claw.workspace.rollback import RollbackError, WorkspaceRollbackManager

_COMMAND_PREFIXES = (
    "/session", "/memory", "/compact", "/workspace", "/approve", "/reject",
    "/approvals", "/skill", "/reflect", "/cron", "/help", "/auto",
    "/unlimited", "/sandbox", "/pi", "/claude", "/pet", "/rollback", "/stop",
    "/exit",
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
    "    status                   记忆统计\n"
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
    "  /sandbox                 查看 sandbox 相关命令的简要说明\n"
    "  /sandbox on              为当前 session 开启沙箱\n"
    "  /sandbox off             为当前 session 关闭沙箱\n"
    "  /sandbox status          查看沙箱状态、可用性与 workspace 类型\n"
    "  Sandbox 说明             未设置 workspace 时使用私有 /workspace；\n"
    "                           设置后仅挂载明确绑定的目录\n"
    "  Workspace 回退（需先设置 workspace）：\n"
    "  /rollback                 回退到上一条用户消息发送前\n"
    "  /rollback <n>             回退到倒数第 n 个用户回合之前\n"
    "  /rollback <checkpointId>  回退到指定检查点\n"
    "  /rollback list            列出当前分支的可用回退点\n"
    "  /rollback status          查看回退状态\n"
    "  /rollback on              开启当前 session 的回退功能\n"
    "  /rollback off             关闭回退并清除已有回退点\n"
    "  /rollback undo            撤销最近一次回退（开始新回合后失效）\n"
    "  /rollback help            查看回退指令说明\n"
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
    "  /unlimited               查看 UNLIMITED 状态和可用指令\n"
    "    status                   查看当前状态\n"
    "    on / off                 开启 / 关闭 UNLIMITED 模式\n"
    "  /pi                      查看 Pi 状态和可用指令\n"
    "    on                       启用 Pi Agent 后端\n"
    "    status                   查看 Pi 状态和可用指令\n"
    "    off                      切回 SJTUClaw 原生后端\n"
    "  /claude                  查看 Claude Code 状态和可用指令\n"
    "    on                       自动检索并启用 Claude Code 后端\n"
    "    status                   查看 Claude Code 状态和可用指令\n"
    "    off                      切回 SJTUClaw 原生后端\n"
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
- `/memory status`：查看记忆统计
- `/reflect status`：查看每日记忆反思状态
- `/reflect enable` / `/reflect disable`：启用或禁用反思
- `/reflect time <HH:MM>`：设置执行时间
- `/reflect now`：立即执行

## Workspace

- `/workspace set <路径>`：设置工作区路径
- `/workspace show`：查看当前工作区
- `/workspace unset`：取消工作区设置

## Sandbox

- `/sandbox`：显示 sandbox 相关命令的简要说明
- `/sandbox status`：查看当前 session 的 sandbox 状态
- `/sandbox on`：为当前 session 开启 sandbox
- `/sandbox off`：为当前 session 关闭 sandbox

> 未设置 workspace 时，原生文件和 Shell 工具使用当前 session 私有的
> `/workspace`；设置 workspace 后，仅挂载用户明确绑定的目录。

## Workspace 回退

> 设置 workspace 不会自动开启回退；请使用 `/rollback on` 显式开启。
> 未设置 workspace 时不支持回退。

- `/rollback`：回退到上一条用户消息发送前
- `/rollback <n>`：回退到倒数第 n 个用户回合之前
- `/rollback <checkpointId>`：回退到指定检查点
- `/rollback list`：列出当前分支的可用回退点
- `/rollback status`：查看回退状态
- `/rollback on`：开启当前 session 的回退功能
- `/rollback off`：关闭回退并清除已有回退点
- `/rollback undo`：撤销最近一次回退；开始新用户回合后 undo 失效
- `/rollback help`：查看回退指令说明

## 审批

- `/approvals`：查看待审批操作
- `/approve [approvalId]`：批准操作
- `/reject [approvalId] [原因]`：拒绝操作

## Agent 模式

- `/auto`：查看 AUTO 状态和可用指令
  - `/auto status`：查看当前状态
  - `/auto on` / `/auto off`：开启或关闭 AUTO 模式
- `/unlimited`：查看 UNLIMITED 状态和可用指令
  - `/unlimited status`：查看当前状态
  - `/unlimited on` / `/unlimited off`：开启或关闭 UNLIMITED 模式
- `/pi` / `/pi status`：查看当前 session 的 Agent 后端和可用指令
- `/pi on`：检查 Pi 运行环境并为当前 session 启用 Pi
- `/pi off`：仅将当前 session 切回 SJTUClaw 原生后端
- `/claude` / `/claude status`：查看 Claude Code 后端状态
- `/claude on`：自动检索本机 Claude Code 并为当前 session 启用
- `/claude off`：仅将当前 session 切回 SJTUClaw 原生后端

> **安全提示：** AUTO 模式会自动批准 workspace 内的结构化文件写入；microsandbox 实际生效时也会自动批准其中的 Shell 操作，宿主 Shell 仍需明确审批。UNLIMITED 模式下所有写入、覆盖、删除和 Shell 操作都需要明确审批。

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
    sandbox_manager: object | None = None
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
    # AUTO mode — skip workspace writes and effective microsandbox shell approval
    auto_mode: bool = False
    # AUTO is session-scoped. Sharing this mapping across command invocations
    # prevents a privileged mode from leaking when the user switches sessions.
    auto_modes: dict[str, bool] = field(default_factory=dict)
    # Optional callbacks for gateway integration
    stop_handler: Callable[[], str] | None = None  # () -> result text
    exit_handler: Callable[[], str] | None = None  # () -> result text
    backend_switcher: Callable[[str], str] | None = None

    def __post_init__(self) -> None:
        if self.auto_mode:
            self.auto_modes[self.current_session_id] = True
        else:
            self.auto_mode = _load_auto_mode(
                self.current_session_id,
                self,
            )


def is_command(user_input: str) -> bool:
    """Return whether input belongs to a known slash-command namespace.

    Unsupported or removed subcommands are still intercepted locally so they
    cannot accidentally be forwarded to the LLM as ordinary chat messages.
    """
    parts = user_input.split(maxsplit=1)
    return bool(parts) and parts[0] in _COMMAND_PREFIXES


def handle_command(user_input: str, state: RuntimeState, *, markdown: bool = False) -> str:
    """Handle a command and return the text to print."""
    def finish(result: str) -> str:
        return _format_command_markdown(result) if markdown else result

    parts = user_input.split()
    if not parts:
        return finish("未知命令：输入为空（输入 /help 查看可用指令）")
    root, *args = parts

    if root == "/session":
        return finish(_handle_session_command(args, state))
    if root == "/memory":
        return finish(_handle_memory_command(args, state))
    if root == "/compact":
        if args:
            return finish("用法: /compact")
        return finish(_handle_compact_command(state))
    if root == "/workspace":
        return finish(_handle_workspace_command(args, state))
    if root == "/sandbox":
        return finish(_handle_sandbox_command(args, state))
    if root == "/rollback":
        return finish(_handle_rollback_command(args, state))
    if root == "/approvals":
        if args:
            return finish("用法: /approvals")
        return finish(_handle_approvals_list(state))
    if root == "/approve":
        return finish(_handle_approve(args, state))
    if root == "/reject":
        return finish(_handle_reject(args, state))
    if root == "/skill":
        return finish(_handle_skill_command(args, state))
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
    if root == "/pi":
        return finish(_handle_pi_command(args, state, markdown=markdown))
    if root == "/claude":
        return finish(_handle_claude_command(args, state, markdown=markdown))
    if root == "/help":
        if args:
            return finish("用法: /help")
        return _HELP_MARKDOWN if markdown else _HELP_TEXT
    if root == "/stop":
        if args:
            return finish("用法: /stop")
        return finish(_handle_stop_command(state))
    if root == "/exit":
        if args:
            return finish("用法: /exit")
        return finish(_handle_exit_command(state))
    return finish(f"未知命令: {root}（输入 /help 查看可用指令）")


def _handle_pi_command(
    args: list[str], state: RuntimeState, *, markdown: bool = False
) -> str:
    """Inspect or explicitly switch the current session's agent backend."""
    if len(args) > 1:
        return "用法: /pi [on|off|status]"
    action = args[0].lower() if args else "status"

    if action in {"status", "show", "help", "?"}:
        if state.backend_switcher is None:
            status = "当前入口不支持读取 Agent 后端状态。"
        else:
            status = state.backend_switcher("status")
        if markdown:
            return (
                "## Pi Agent 后端\n\n"
                f"**{status}**\n\n"
                "### 可用指令\n\n"
                "- `/pi on`：检查运行环境并为当前 session 启用 Pi\n"
                "- `/pi off`：将当前 session 切回 SJTUClaw 原生后端\n"
                "- `/pi status`：查看当前状态和可用指令\n\n"
                "> Pi 模式通过官方 RPC 接入 Pi coding agent，保留其工具循环、"
                "Skills、自动压缩和持久会话，并继续使用 SJTUClaw 的界面与审批流程。"
            )
        return (
            f"{status}\n\n"
            "可用指令：\n"
            "  /pi on      检查运行环境并为当前 session 启用 Pi\n"
            "  /pi off     将当前 session 切回 SJTUClaw 原生后端\n"
            "  /pi status  查看当前状态和可用指令\n\n"
            "Pi 模式通过官方 RPC 接入 Pi coding agent，保留其工具循环、Skills、"
            "自动压缩和持久会话，并继续使用 SJTUClaw 的界面与审批流程。"
        )

    target = {
        "on": "pi", "enable": "pi",
        "off": "sjtuclaw", "disable": "sjtuclaw",
    }.get(action, action)
    if target not in {"pi", "sjtuclaw"}:
        return "用法: /pi [on|off|status]"
    if state.backend_switcher is None:
        return "[错误] 当前入口不支持运行时切换 Agent 后端。"
    return state.backend_switcher(target)


def _handle_claude_command(
    args: list[str], state: RuntimeState, *, markdown: bool = False
) -> str:
    """Inspect or explicitly switch the current session to Claude Code."""
    if len(args) > 1:
        return "用法: /claude [on|off|status]"
    action = args[0].lower() if args else "status"

    if action in {"status", "show", "help", "?"}:
        if state.backend_switcher is None:
            status = "当前入口不支持读取 Agent 后端状态。"
        else:
            status = state.backend_switcher("status")
        if markdown:
            return (
                "## Claude Code 后端\n\n"
                f"**{status}**\n\n"
                "### 可用指令\n\n"
                "- `/claude on`：自动检索本机 Claude Code 并为当前 session 启用\n"
                "- `/claude off`：将当前 session 切回 SJTUClaw 原生后端\n"
                "- `/claude status`：查看当前状态和可用指令\n\n"
                "> Claude Code 模式沿用本机现有登录、模型、Skills、MCP 与权限配置，"
                "并通过官方 stream-json 接口保留工具循环和持久会话。"
            )
        return (
            f"{status}\n\n"
            "可用指令：\n"
            "  /claude on      自动检索并启用 Claude Code\n"
            "  /claude off     切回 SJTUClaw 原生后端\n"
            "  /claude status  查看当前状态和可用指令\n\n"
            "Claude Code 模式沿用本机现有登录、模型、Skills、MCP 与权限配置，"
            "并保留工具循环和持久会话。"
        )

    target = {
        "on": "claude",
        "enable": "claude",
        "off": "sjtuclaw",
        "disable": "sjtuclaw",
    }.get(action, action)
    if target not in {"claude", "sjtuclaw"}:
        return "用法: /claude [on|off|status]"
    if state.backend_switcher is None:
        return "[错误] 当前入口不支持运行时切换 Agent 后端。"
    return state.backend_switcher(target)


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
        if stripped in {
            "用法:", "用法：", "可用指令:", "可用指令：",
            "相关指令:", "相关指令：",
        }:
            formatted.append("### 可用指令")
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
        usage_parts = re.split(r"\s{2,}", stripped, maxsplit=1)
        if stripped.startswith("/") and len(usage_parts) == 2:
            syntax, description = usage_parts
            formatted.append(f"- `{syntax}`：{description}")
            continue
        if indent >= 4:
            formatted.append(f"  - {stripped}")
        elif indent >= 1:
            formatted.append(f"- {stripped}")
        else:
            formatted.append(line)
    return "\n".join(formatted)


_SKILL_INVOKE_PREFIX = "__SKILL_INVOKE__|"


def parse_skill_invoke_result(result: str) -> tuple[str, str] | None:
    """Decode the internal explicit-skill command result for entrypoints.

    ``handle_command`` stays text-based for backwards compatibility, but CLI
    and Gateway callers must consume this marker instead of displaying it.
    The task portion may itself contain ``|``, so split at most twice.
    """
    if not result.startswith(_SKILL_INVOKE_PREFIX):
        return None
    parts = result.split("|", 2)
    if len(parts) != 3:
        return None
    skill_name, task = parts[1].strip(), parts[2].strip()
    if not skill_name or not task:
        return None
    return skill_name, task


# -- /session ---------------------------------------------------------------


_SESSION_USAGE = """用法:
  /session new                  创建并切换到新会话
  /session list                 列出所有会话和消息数量
  /session switch <sessionId>   切换到指定会话
  /session rename <id> <标题>   修改会话标题
  /session delete <sessionId>   删除指定会话"""


def _handle_session_command(args: list[str], state: RuntimeState) -> str:
    if not args or args[0].lower() in {"help", "?"}:
        return _SESSION_USAGE

    sub, rest = args[0], args[1:]

    if sub == "new":
        if rest:
            return "用法: /session new"
        session = state.session_store.create_session()
        _activate_session(session.session_id, state)
        return f"Created session: {session.session_id}"

    if sub == "list":
        if rest:
            return "用法: /session list"
        return _format_session_list(state)

    if sub == "switch":
        if len(rest) != 1:
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
        if len(rest) != 1:
            return "用法: /session delete <sessionId>"
        return _delete_session(rest[0], state)

    return f"未知 /session 子命令: {sub}\n\n{_SESSION_USAGE}"


def _switch_session(session_id: str, state: RuntimeState) -> str:
    try:
        state.session_store.get(session_id)
    except SessionNotFoundError as exc:
        return f"[错误] {exc}"
    _activate_session(session_id, state)
    return f"Switched to session: {session_id}"


def _delete_session(session_id: str, state: RuntimeState) -> str:
    guard = (
        state.rollback_manager.session_guard(session_id)
        if state.rollback_manager is not None else nullcontext()
    )
    try:
        with guard:
            # Delete the primary record first. If this fails, auxiliary
            # workspace/rollback state must remain intact.
            state.session_store.delete(session_id)
    except (SessionNotFoundError, SessionStoreError, OSError, RollbackError) as exc:
        return f"[错误] {exc}"

    cleanup_warnings: list[str] = []
    if state.rollback_manager is not None:
        try:
            state.rollback_manager.purge(session_id)
        except (RollbackError, OSError) as exc:
            cleanup_warnings.append(f"回退数据清理失败: {exc}")
    if state.workspace_manager is not None:
        try:
            state.workspace_manager.unset(session_id)
        except (WorkspaceError, OSError) as exc:
            cleanup_warnings.append(f"workspace 绑定清理失败: {exc}")
        finally:
            state.workspace_manager.set_unlimited(session_id, False)
    if state.sandbox_manager is not None:
        try:
            state.sandbox_manager.purge_session(session_id)
        except Exception as exc:
            cleanup_warnings.append(f"sandbox 清理失败: {exc}")
    state.auto_modes.pop(session_id, None)

    suffix = (
        "\n[警告] " + "；".join(cleanup_warnings)
        if cleanup_warnings
        else ""
    )
    if state.current_session_id != session_id:
        return f"Deleted session: {session_id}{suffix}"

    summaries = state.session_store.list_summaries()
    if summaries:
        fallback_id = summaries[0].session_id
    else:
        fallback_id = state.session_store.ensure_default_session().session_id
    _activate_session(fallback_id, state)
    return (
        f"Deleted session: {session_id}\n"
        f"当前 session 已被删除，已自动切换到: {fallback_id}{suffix}"
    )


def _activate_session(session_id: str, state: RuntimeState) -> None:
    """Switch session and restore only that session's persisted AUTO mode."""
    state.current_session_id = session_id
    state.auto_mode = _load_auto_mode(session_id, state)


def _set_auto_mode(state: RuntimeState, enabled: bool) -> None:
    if state.session_store.exists(state.current_session_id):
        state.session_store.set_metadata_flag(
            state.current_session_id,
            AUTO_MODE_METADATA_KEY,
            enabled,
        )
    state.auto_mode = enabled
    if enabled:
        state.auto_modes[state.current_session_id] = True
    else:
        state.auto_modes.pop(state.current_session_id, None)


def _load_auto_mode(session_id: str, state: RuntimeState) -> bool:
    if state.auto_modes.get(session_id) is True:
        return True
    if not state.session_store.exists(session_id):
        return False
    enabled = (
        state.session_store.get_metadata_flag(
            session_id,
            AUTO_MODE_METADATA_KEY,
        )
        is True
    )
    if enabled:
        state.auto_modes[session_id] = True
    else:
        state.auto_modes.pop(session_id, None)
    return enabled


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


_MEMORY_USAGE = """用法:
  /memory add [选项] <内容>          添加一条长期记忆
  /memory list [--category <类别>]   列出记忆，可按类别筛选
  /memory search <关键词>            搜索相关记忆
  /memory update <memoryId> <内容>   更新指定记忆
  /memory delete <memoryId>          删除指定记忆
  /memory status                     查看记忆数量统计

添加选项: --category <类别>、--tags <t1,t2>、--importance <1-5>"""


def _handle_memory_command(args: list[str], state: RuntimeState) -> str:
    if not args or args[0].lower() in {"help", "?"}:
        return _MEMORY_USAGE

    sub = args[0]

    if sub == "add":
        return _add_memory(args[1:], state)

    if sub == "list":
        return _format_memory_list(args[1:], state)

    if sub == "search":
        if len(args) < 2:
            return "用法: /memory search <关键词>"
        return _search_memory(" ".join(args[1:]), state)

    if sub == "status":
        if len(args) != 1:
            return "用法: /memory status"
        return _memory_stats(state)

    if sub == "update":
        if len(args) < 3:
            return "用法: /memory update <memoryId> <新内容>"
        memory_id = args[1]
        new_content = " ".join(args[2:])
        return _update_memory(memory_id, new_content, state)

    if sub == "delete":
        if len(args) != 2:
            return "用法: /memory delete <memoryId>"
        return _delete_memory(args[1], state)

    return f"未知 /memory 子命令: {sub}\n\n{_MEMORY_USAGE}"


_MEMORY_ADD_USAGE = (
    "用法: /memory add [--category <类别>] [--tags <t1,t2>] "
    "[--importance <1-5>] <内容>"
)


def _add_memory(tokens: list[str], state: RuntimeState) -> str:
    """Parse and add a memory entry.

    Options may appear in any order before the content. ``--`` explicitly
    ends option parsing when the memory text itself starts with ``--``.
    """
    category = "general"
    tags: list[str] = []
    importance = 3
    index = 0
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        if not option.startswith("--"):
            break
        if option not in {"--category", "--tags", "--importance"}:
            return f"[错误] 未知选项: {option}\n{_MEMORY_ADD_USAGE}"
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            return f"[错误] {option} 缺少参数\n{_MEMORY_ADD_USAGE}"
        value = tokens[index + 1]
        if option == "--category":
            category = value
        elif option == "--tags":
            tags = [tag.strip() for tag in value.split(",") if tag.strip()]
        else:
            try:
                importance = int(value)
            except ValueError:
                return f"[错误] importance 必须是 1-5 的整数\n{_MEMORY_ADD_USAGE}"
        index += 2

    content = " ".join(tokens[index:]).strip()
    if not content:
        return _MEMORY_ADD_USAGE

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
    if extra_args:
        if len(extra_args) != 2 or extra_args[0] != "--category":
            return "用法: /memory list [--category <类别>]"
        category = extra_args[1]

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
    external_compact = getattr(state.llm_client, "compact_session", None)
    backend_resolver = getattr(state.llm_client, "backend_for_session", None)
    backend = (
        backend_resolver(state.current_session_id, state.session_store)
        if callable(backend_resolver)
        else ("pi" if callable(external_compact) else "sjtuclaw")
    )
    session_uses_external = backend in {"pi", "claude"}
    backend_label = "Pi" if backend == "pi" else "Claude Code"
    external_error: str | None = None
    if session_uses_external and callable(external_compact):
        try:
            return external_compact(
                state.current_session_id,
                session_store=state.session_store,
            )
        except Exception as exc:
            # Preserve native compaction as the first choice, then fall back
            # to the host summary projection when possible.
            external_error = str(exc)

    session = state.session_store.get(state.current_session_id)

    if len(session.messages) <= KEEP_RECENT_MESSAGES_MIN:
        result = (
            f"当前 session 只有 {len(session.messages)} 条消息，"
            f"不超过保留窗口（{KEEP_RECENT_MESSAGES_MIN}），无需压缩。"
        )
        if external_error:
            return f"{backend_label} 原生压缩未执行：{external_error}\n{result}"
        return result

    worker_running = getattr(state.compaction_worker, "is_running", None)
    if callable(worker_running) and worker_running():
        return "后台压缩正在进行，请等待完成后再执行 /compact。"

    try:
        outcome = compact_and_persist(
            session,
            state.session_store,
            state.llm_client,
            force=True,
        )
    except CompactionError as exc:
        if external_error:
            return (
                f"[错误] {backend_label} 原生压缩未完成：{external_error}\n"
                f"SJTUClaw 回退压缩也未完成：{exc}"
            )
        return f"[错误] {exc}"

    result = outcome.result
    lines: list[str] = []
    if external_error:
        lines.extend([
            f"{backend_label} 原生压缩未执行：{external_error}",
            "已回退到 SJTUClaw 统一会话压缩。",
            "",
        ])
    lines.append(format_compaction_brief(session.session_id, result))
    # Retain the original compact status line for terminal/API clients that
    # used it as a lightweight success marker before structured briefs existed.
    lines.append(f"\nCompacted session {session.session_id}.")
    lines.append(f"Old messages: {result.old_message_count}")
    lines.append(f"Recent messages: {result.recent_message_count}")
    lines.append("Summary updated: yes")
    if outcome.save_error:
        lines.append(f"[警告] 压缩结果保存可能未成功: {outcome.save_error}")
    return "\n".join(lines)


# -- /workspace (Step 8) -------------------------------------------------


_WORKSPACE_USAGE = """用法:
  /workspace set <路径>   设置当前 session 的工作区
  /workspace show         查看当前工作区路径
  /workspace unset        取消工作区并关闭 UNLIMITED 模式

Workspace 用于限制文件操作范围；设置后不会自动开启回退。
如需回退功能，请使用 /rollback on 显式开启。"""


def _handle_workspace_command(args: list[str], state: RuntimeState) -> str:
    if not args or args[0].lower() in {"help", "?"}:
        return _WORKSPACE_USAGE

    if state.workspace_manager is None:
        return "Workspace manager 未初始化。"

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
                rollback_preference = (
                    state.rollback_manager.preference(sid)
                    if state.rollback_manager is not None else None
                )
                resolved = state.workspace_manager.set(sid, path_str)
                if state.sandbox_manager is not None:
                    state.sandbox_manager.close_session(sid)
                if (
                    state.rollback_manager is not None
                    and rollback_preference is True
                ):
                    state.rollback_manager.enable(
                        sid,
                        state.session_store.get(sid),
                        explicit=False,
                    )
            return f"Workspace 已设置为: {resolved}"
        except (WorkspaceError, RollbackError, SandboxError, OSError) as exc:
            try:
                if previous is None:
                    state.workspace_manager.unset(sid)
                else:
                    state.workspace_manager.set(sid, str(previous))
            except (WorkspaceError, OSError):
                pass
            return f"[错误] {exc}"

    if sub == "show":
        if len(args) != 1:
            return "用法: /workspace show"
        ws = state.workspace_manager.get(sid)
        if ws is None:
            return "当前 session 未设置 workspace。使用 /workspace set <路径> 来设置。"
        return f"当前 workspace: {ws}"

    if sub == "unset":
        if len(args) != 1:
            return "用法: /workspace unset"
        guard = (
            state.rollback_manager.session_guard(sid)
            if state.rollback_manager is not None else nullcontext()
        )
        try:
            with guard:
                if state.sandbox_manager is not None:
                    state.sandbox_manager.close_session(sid)
                if state.rollback_manager is not None:
                    state.rollback_manager.purge(sid)
                state.workspace_manager.unset(sid)
                # Removing the sandbox root must never leave unrestricted
                # filesystem access enabled implicitly.
                state.workspace_manager.set_unlimited(sid, False)
        except (WorkspaceError, RollbackError, SandboxError, OSError) as exc:
            return f"[错误] {exc}"
        return "Workspace 已取消设置，UNLIMITED 模式已关闭。"

    return f"未知 /workspace 子命令: {sub}\n\n{_WORKSPACE_USAGE}"


_SANDBOX_USAGE = """用法:
  /sandbox          显示 sandbox 相关命令
  /sandbox status   查看模式、可用性、运行状态与 workspace 类型
  /sandbox on       为当前 session 开启 sandbox
  /sandbox off      为当前 session 关闭 sandbox

未设置 workspace 时使用当前 session 私有的 /workspace；设置后仅挂载明确绑定的目录。"""


def _handle_sandbox_command(args: list[str], state: RuntimeState) -> str:
    """Show or change the current session's isolated runtime."""
    if not args:
        return _SANDBOX_USAGE
    if len(args) != 1:
        return _SANDBOX_USAGE
    sub = args[0].lower()
    if sub not in {"status", "show", "on", "off"}:
        return f"未知 /sandbox 子命令: {sub}\n\n{_SANDBOX_USAGE}"
    manager = state.sandbox_manager
    if manager is None or state.workspace_manager is None:
        return "Sandbox manager 未初始化。"
    sid = state.current_session_id
    if sub in {"status", "show"}:
        status = manager.status(sid, state.workspace_manager)
        enabled = "已开启" if status["enabled"] else "已关闭"
        state_source = (
            "session 持久设置"
            if status.get("preference") is not None
            else "配置默认值"
        )
        availability = (
            "运行环境可用" if status["available"] else "运行环境不可用"
        )
        running = (
            "运行中"
            if status["running"]
            else ("按需启动" if status["effective"] else "未运行")
        )
        kind = (
            "已绑定宿主 workspace"
            if status["workspaceKind"] == "host-mounted"
            else "sandbox 私有 workspace"
        )
        python_environment = (
            f"项目依赖: {status['projectVenv']}（自动同步，复用镜像通用库）\n"
            if status.get("projectVenv")
            else "项目依赖: 使用镜像默认环境\n"
        )
        return (
            f"Sandbox 状态: {enabled}（{state_source}，"
            f"配置模式 {status['mode']}，"
            f"{availability}，{running}，"
            f"{'当前生效' if status['effective'] else '当前未生效'}）\n"
            f"Agent 后端: {status['agentBackend']}（"
            f"{'已覆盖' if status['covered'] else '未覆盖'}）\n"
            f"Workspace: {kind} → {status['guestWorkspace']}\n"
            f"镜像: {status['image']}\n"
            f"{python_environment}"
            f"网络: {status['network']}；安全策略: {status['security']}"
        )
    try:
        manager.set_session_enabled(
            sid,
            sub == "on",
            state.workspace_manager,
        )
    except SandboxError as exc:
        return f"[错误] sandbox 状态修改失败: {exc}"
    if sub == "on":
        return (
            "当前 session 的 sandbox 已开启。"
            "原生文件和 Shell 工具将在隔离 microVM 中运行；"
            "该状态会随 session 持久保存；首次启动会创建运行 venv，"
            "并使用 /workspace/.venv 持久化项目依赖。"
        )
    return (
        "当前 session 的 sandbox 已关闭，已停止其 microVM。"
        "该状态会随 session 持久保存；"
        "其他 session 的 sandbox 状态不受影响。"
    )


_ROLLBACK_USAGE = """用法:
  /rollback                  回退到上一条用户消息发送前
  /rollback <n>              回退到倒数第 n 个用户回合之前
  /rollback <checkpointId>   回退到指定检查点
  /rollback list             列出当前分支的可用回退点
  /rollback status           查看回退状态
  /rollback on               开启当前 session 的回退功能
  /rollback off              关闭回退并清除已有回退点
  /rollback undo             撤销最近一次回退
  /rollback help             查看这些指令

回退需要先设置 workspace，但设置 workspace 不会自动开启回退；
请使用 /rollback on 显式开启。回退会同时恢复会话和工作区文件。
注意：rollback功能仍不完善，workspace中文件过多时不建议使用。"""


def _handle_rollback_command(args: list[str], state: RuntimeState) -> str:
    if args and args[0].lower() in {"help", "?"}:
        return _ROLLBACK_USAGE
    manager = state.rollback_manager
    if manager is None:
        return "Workspace 回退服务未初始化。"
    sid = state.current_session_id
    if len(args) > 1:
        return "用法: /rollback [<n>|<checkpointId>|list|status|on|off|undo]"
    sub = args[0].lower() if args else "1"
    try:
        if sub in ("status", "show"):
            status = manager.status(sid)
            if not status["enabled"]:
                if status.get("workspace") and status.get("preference") is False:
                    return (
                        "Workspace 回退已关闭。\n"
                        f"路径: {status['workspace']}\n"
                        "使用 /rollback on 重新开启。"
                    )
                if status.get("workspace"):
                    return "当前 session 未启用回退。使用 /rollback on 开启。"
                return "当前 session 未启用回退。请先设置 workspace。"
            return (
                f"Workspace 回退已启用。\n"
                f"路径: {status['workspace']}\n"
                f"回退点: {status['checkpointCount']}\n"
                f"其中部分快照: {status.get('partialCheckpointCount', 0)}\n"
                f"可撤销回退: {'是' if status['undoAvailable'] else '否'}"
            )
        if sub == "on":
            if manager.workspace_manager.get(sid) is None:
                return "[错误] 当前 session 未设置 workspace。请先使用 /workspace set <路径>。"
            status = manager.enable(sid, state.session_store.get(sid))
            return (
                "Workspace 回退已开启。\n"
                f"路径: {status['workspace']}\n"
                "后续用户回合将创建新的回退点。"
            )
        if sub == "off":
            if manager.workspace_manager.get(sid) is None:
                return "[错误] 当前 session 未设置 workspace。请先使用 /workspace set <路径>。"
            manager.disable(sid)
            return "Workspace 回退已关闭，已有回退点已清除。"
        if sub == "list":
            status = manager.status(sid)
            if status.get("preference") is False:
                return "Workspace 回退已关闭。使用 /rollback on 重新开启。"
            checkpoints = [item for item in manager.list_checkpoints(sid) if item["kind"] == "turn"]
            if not checkpoints:
                return "当前没有可用的消息回退点。"
            lines = ["可用回退点："]
            for index, item in enumerate(checkpoints, 1):
                warning = " [部分快照]" if item["partial"] else ""
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
        warning_lines = list(result.get("warnings", []))
        if result["partial"]:
            warning_lines.append(
                "该回退点为部分快照；超出 Workspace 或快照预算的改动可能未恢复。"
            )
        warning = (
            "\n注意：" + "；".join(dict.fromkeys(warning_lines))
            if warning_lines else ""
        )
        return (
            f"回退完成。恢复 {result['restored']} 个路径，"
            f"删除 {result['deleted']} 个路径。可使用 /rollback undo 撤销。{warning}"
        )
    except (RollbackError, WorkspaceError, OSError) as exc:
        return f"[错误] {exc}"


# -- /approvals (list pending) ---------------------------------------------


_APPROVAL_USAGE = """相关指令:
  /approvals                  查看当前 session 的待审批操作
  /approve [approvalId]       批准操作；仅有一项时可省略 ID
  /reject [approvalId] [原因]  拒绝操作；仅有一项时可省略 ID"""


def _handle_approvals_list(state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    pending = [r for r in state.approval_manager.get_pending()
               if r.session_id == state.current_session_id]
    if not pending:
        return f"当前没有待审批的操作。\n\n{_APPROVAL_USAGE}"

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
    lines.extend(["", _APPROVAL_USAGE])
    return "\n".join(lines)


# -- /approve --------------------------------------------------------------


def _handle_approve(args: list[str], state: RuntimeState) -> str:
    if state.approval_manager is None:
        return "Approval manager 未初始化。"

    if len(args) > 1:
        return "用法: /approve <approvalId>（或当只有一个待审批时省略 ID）"
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


def _handle_reject(args: list[str], state: RuntimeState) -> str:
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


_SKILL_USAGE = """用法:
  /skill list                    列出可用 Skills 及其简介
  /skill show <skill-name>       查看指定 Skill 的详细说明
  /skill usage                   查看当前 session 的 Skill 使用记录
  /skill <skill-name> <任务描述>   使用指定 Skill 执行任务

Skill 是可复用的专业工作流；先用 /skill list 查看当前可用项。"""


def _handle_skill_command(args: list[str], state: RuntimeState) -> str:
    """Handle /skill commands.  Returns either a plain result string or
    a special ``__SKILL_INVOKE__`` sentinel indicating that the caller
    should run an agent turn with the skill content pre-loaded.
    """
    if not args or args[0].lower() in {"help", "?"}:
        return _SKILL_USAGE

    if state.skill_registry is None:
        return "Skill registry 未初始化。"

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
        return (
            f"未找到 skill: \"{skill_name}\"。使用 /skill list 查看可用 skill。\n\n"
            f"{_SKILL_USAGE}"
        )

    task = " ".join(args[1:]).strip()
    if not task:
        return f"用法: /skill {skill_name} <任务描述>"

    # Return sentinel — the REPL will detect this and call run_agent_turn
    # with the skill pre-loaded.
    return f"__SKILL_INVOKE__|{skill_name}|{task}"


# -- /reflect (daily memory reflection) --------------------------------------


_REFLECT_USAGE = """用法:
  /reflect status        查看启用状态、执行时间和上次结果
  /reflect enable        启用每日记忆反思
  /reflect disable       禁用每日记忆反思
  /reflect time <HH:MM>  设置每天执行时间
  /reflect now           立即整理近期会话并提取长期记忆"""


def _handle_reflect_command(args: list[str], state: RuntimeState) -> str:
    """Handle /reflect commands for daily memory reflection config."""
    if not args or args[0].lower() in {"help", "?"}:
        return _REFLECT_USAGE

    if state.reflection_manager is None:
        return "Reflection manager 未初始化。"

    mgr = state.reflection_manager

    sub = args[0]

    if sub == "status":
        if len(args) != 1:
            return "用法: /reflect status"
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
        if len(args) != 1:
            return "用法: /reflect enable"
        mgr.update_config(enabled=True)
        return "✅ 每日记忆反思已启用。每天定时自动整理对话，提取长期记忆。"

    if sub == "disable":
        if len(args) != 1:
            return "用法: /reflect disable"
        mgr.update_config(enabled=False)
        return "❌ 每日记忆反思已禁用。"

    if sub == "time":
        if len(args) != 2:
            return "用法: /reflect time <HH:MM>（如 /reflect time 23:00）"
        new_time = args[1]
        if not is_valid_reflection_time(new_time):
            return "时间无效，请使用 00:00 到 23:59 的 HH:MM 格式"
        mgr.update_config(time=new_time)
        return f"⏰ 每日反思时间已设置为 {new_time}。"

    if sub == "now":
        if len(args) != 1:
            return "用法: /reflect now"
        result = mgr.run_now()
        if result.get("ok"):
            return (
                f"✅ 即时反思完成。\n"
                f"  检查了 {result.get('sessionsReviewed', 0)} 个 session\n"
                f"  提取了 {result.get('factsExtracted', 0)} 条记忆"
            )
        else:
            return f"❌ 反思失败: {result.get('error', '未知错误')}"

    return f"未知 /reflect 子命令: {sub}\n\n{_REFLECT_USAGE}"


# -- /cron ------------------------------------------------------------------


_CRON_USAGE = """用法:
  /cron list                 列出所有定时作业
  /cron status               查看 Cron 服务状态
  /cron disable <jobId>      禁用指定作业
  /cron enable <jobId>       启用指定作业
  /cron delete <jobId>       删除指定作业"""


def _handle_cron_command(args: list[str], state: RuntimeState) -> str:
    """Handle /cron commands."""
    if not args:
        return _CRON_USAGE

    if state.cron_service is None:
        return "Cron 服务未初始化。"

    sub = args[0]

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
        if len(args) != 1:
            return "用法: /cron status"
        status = state.cron_service.status()
        return (
            f"Cron 服务: {'运行中' if status['enabled'] else '已停止'}\n"
            f"作业数: {status['jobs']}"
        )

    if sub == "disable":
        if len(args) != 2:
            return "用法: /cron disable <jobId>"
        job = state.cron_service.enable_job(args[1], enabled=False)
        if job is None:
            return f"作业不存在: {args[1]}"
        return f"已禁用作业: {args[1]}"

    if sub == "enable":
        if len(args) != 2:
            return "用法: /cron enable <jobId>"
        job = state.cron_service.enable_job(args[1], enabled=True)
        if job is None:
            return f"作业不存在: {args[1]}"
        return f"已启用作业: {args[1]}"

    if sub == "delete":
        if len(args) != 2:
            return "用法: /cron delete <jobId>"
        result = state.cron_service.remove_job(args[1])
        if result == "removed":
            return f"已删除作业: {args[1]}"
        if result == "protected":
            return f"无法删除受保护的系统作业: {args[1]}"
        return f"作业不存在: {args[1]}"

    return _CRON_USAGE


# -- /pet -------------------------------------------------------------------


_PET_USAGE = """用法:
  /pet status                 查看运行状态和当前角色
  /pet list                   列出所有可用宠物
  /pet open                   开启桌面宠物
  /pet close                  关闭桌面宠物
  /pet select <petId>         切换宠物角色
  /pet autostart <on|off>     设置是否随 Gateway 启动"""


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
            f"{_PET_USAGE}"
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
        if len(args) != 1:
            return "用法: /pet open"
        catalog.update_settings(enabled=True)
        started = process.start()
        if process.running:
            return "桌面宠物已开启。"
        return "已提交桌面宠物启动请求。" if started else "桌面宠物已经在运行。"

    if sub in ("close", "off", "disable"):
        if len(args) != 1:
            return "用法: /pet close"
        catalog.update_settings(enabled=False)
        process.stop()
        return "桌面宠物已关闭。"

    if sub == "select":
        if len(args) != 2:
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
        if len(args) != 2 or args[1].lower() not in ("on", "off"):
            return "用法: /pet autostart <on|off>"
        enabled = args[1].lower() == "on"
        catalog.update_settings(launch_on_gateway_start=enabled)
        return f"随 Gateway 启动已{'开启' if enabled else '关闭'}。"

    return f"未知 /pet 子命令: {sub}\n\n{_PET_USAGE}"

# -- /auto ------------------------------------------------------------------


def _handle_auto_command(
    args: list[str], state: RuntimeState, *, markdown: bool = False
) -> str:
    """Inspect or explicitly change AUTO mode."""
    if len(args) > 1:
        return "用法: /auto [status|on|off]"
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
                "- `/auto status`：查看当前状态\n\n"
                "> AUTO 模式会自动批准 workspace 内的结构化文件写入；"
                "microsandbox 实际生效时也会自动批准其中的 Shell 操作，"
                "Pi / Claude Code 的原生危险工具也会自动批准；"
                "宿主 Shell 仍需明确审批。UNLIMITED 模式下所有危险操作"
                "也仍需用户明确审批。AUTO 状态会随当前 session 持久保存。"
            )
        return (
            f"AUTO 模式当前{state_text}。\n\n"
            "可用指令：\n"
            "  /auto on      开启 AUTO 模式\n"
            "  /auto off     关闭 AUTO 模式\n"
            "  /auto status  查看当前状态\n\n"
            "AUTO 模式会自动批准 workspace 内的结构化文件写入；"
            "microsandbox 实际生效时也会自动批准其中的 Shell 操作，"
            "Pi / Claude Code 的原生危险工具也会自动批准；"
            "宿主 Shell 仍需明确审批。UNLIMITED 模式下所有危险操作"
            "也仍需用户明确审批。AUTO 状态会随当前 session 持久保存。"
        )

    if sub in ("on", "enable", "1"):
        try:
            _set_auto_mode(state, True)
        except (SessionStoreError, OSError) as exc:
            return f"[错误] AUTO 状态保存失败: {exc}"
        return (
            "AUTO 模式已开启并保存到当前 session。"
            "workspace 内的结构化文件写入可自动执行；"
            "microsandbox 实际生效时其中的 Shell 命令也可自动执行，"
            "Pi / Claude Code 的原生危险工具也可自动执行；"
            "宿主 Shell 仍需逐一审批。"
        )
    elif sub in ("off", "disable", "0"):
        try:
            _set_auto_mode(state, False)
        except (SessionStoreError, OSError) as exc:
            return f"[错误] AUTO 状态保存失败: {exc}"
        return (
            "AUTO 模式已关闭并保存到当前 session。"
            "Agent 的写入和 Shell 操作恢复逐次审批。"
        )

    return (
        f"未知的 AUTO 子指令：{sub}\n"
        "请使用 /auto 查看帮助，或使用 /auto on、/auto off。"
    )


# -- /unlimited -------------------------------------------------------------


def _handle_unlimited_command(
    args: list[str], state: RuntimeState, *, markdown: bool = False
) -> str:
    """Show or change UNLIMITED mode for the current session."""
    if state.workspace_manager is None:
        return "Workspace manager 未初始化，无法使用此功能。"
    if len(args) > 1:
        return "用法: /unlimited [status|on|off]"
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
                "\n"
                "> **安全规则：** UNLIMITED 只解除路径限制；写入、覆盖、删除和 "
                "Shell 操作仍必须由用户逐次审批，AUTO 模式不能绕过审批。"
            )
        return (
            f"UNLIMITED 模式当前{status}。\n\n"
            "可用指令：\n"
            "- `/unlimited status`：查看当前状态\n"
            "- `/unlimited on`：开启模式，允许访问 workspace 之外的路径\n"
            "- `/unlimited off`：关闭模式，恢复 workspace 边界限制\n"
            "\n"
            "安全规则：UNLIMITED 只解除路径限制；写入、覆盖、删除和 shell 操作"
            "仍必须由用户逐次审批，AUTO 模式不能绕过审批。"
        )

    if sub in ("help", "status", "show"):
        return _help()
    if sub in ("on", "enable", "1"):
        if (
            state.sandbox_manager is not None
            and state.sandbox_manager.required
        ):
            return (
                "[错误] required sandbox 模式不允许开启 UNLIMITED；"
                "该模式会绕过 microVM 的宿主文件隔离。"
            )
        if state.sandbox_manager is not None:
            try:
                state.sandbox_manager.close_session(sid)
            except SandboxError as exc:
                return f"[错误] 无法关闭当前 sandbox: {exc}"
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
