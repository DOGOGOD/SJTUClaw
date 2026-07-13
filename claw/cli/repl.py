"""Interactive terminal REPL for multi-turn conversation with claw.

This module owns terminal input/output only.
"""

from __future__ import annotations

import json

from claw.agent.loop import run_agent_turn
from claw.approval.manager import ApprovalManager, ApprovalRequest
from claw.cli.commands import RuntimeState, handle_command, is_command
from claw.context.builder import ContextBuilder
from claw.llm.client import LLMClient, LLMError
from claw.memory.store import MemoryStore
from claw.session.store import SessionStore
from claw.session.title import auto_title_if_first_turn
from claw.tools.base import ToolRegistry
from claw.tools import register_all_tools
from claw.workspace.manager import WorkspaceManager

EXIT_COMMANDS = {"/exit"}
WELCOME_MESSAGE = "claw started. Type /exit to quit."


def run_repl(
    client: LLMClient,
    session_store: SessionStore,
    memory_store: MemoryStore,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
    *,
    workspace_manager: WorkspaceManager | None = None,
    approval_manager: ApprovalManager | None = None,
    skill_registry=None,
    reflection_manager=None,
    compaction_worker=None,
    llm_config=None,
    cron_service=None,
    pet_catalog=None,
    pet_process=None,
) -> None:
    """Run the interactive multi-turn conversation loop."""
    initial_session = session_store.ensure_default_session()
    state = RuntimeState(
        session_store=session_store,
        memory_store=memory_store,
        llm_client=client,
        current_session_id=initial_session.session_id,
        workspace_manager=workspace_manager,
        approval_manager=approval_manager,
        tool_registry=tool_registry,
        reflection_manager=reflection_manager,
        compaction_worker=compaction_worker,
        llm_config=llm_config,
        cron_service=cron_service,
        pet_catalog=pet_catalog,
        pet_process=pet_process,
    )

    # Register advanced tools with the current session id provider
    if workspace_manager is not None:
        fresh_registry = ToolRegistry()
        register_all_tools(
            fresh_registry,
            workspace_manager=workspace_manager,
            session_id_provider=lambda: state.current_session_id,
            sessions_dir=session_store.sessions_dir,
            include_skill_tool=skill_registry is not None,
            skill_registry=skill_registry,
            include_memory_tools=True,
            memory_store=memory_store,
        )
        tool_registry.clear()
        for name in fresh_registry.list_tool_names():
            tool_registry.register(fresh_registry.get_tool(name))

    # Register memory + cron + skill tools when workspace not available
    if workspace_manager is None:
        from claw.tools.memory_tools import create_recall_tool, create_remember_tool

        try:
            tool_registry.register(
                create_remember_tool(memory_store, lambda: state.current_session_id)
            )
        except Exception:
            pass
        try:
            tool_registry.register(create_recall_tool(memory_store))
        except Exception:
            pass

        # Skill tools
        if skill_registry is not None:
            from claw.tools.skills_tool import create_skills_list_tool, create_skill_view_tool
            from claw.tools.skill_manager_tool import create_skill_manage_tool

            try:
                tool_registry.register(create_skills_list_tool(skill_registry))
            except Exception:
                pass
            try:
                tool_registry.register(create_skill_view_tool(skill_registry))
            except Exception:
                pass
            try:
                tool_registry.register(create_skill_manage_tool(skill_registry))
            except Exception:
                pass

    # Register cron tool if available
    if cron_service is not None:
        from claw.tools.cron_tool import CronTool

        cron_tool = CronTool(cron_service)
        cron_tool.set_context(
            session_key=initial_session.session_id,
            channel="cli",
            chat_id=initial_session.session_id,
        )
        tool_registry.register(cron_tool)

    # Build the approval handler for CLI
    def _cli_approval_handler(req: ApprovalRequest) -> ApprovalRequest:
        if approval_manager is None:
            return req

        approval_manager.register(req)

        args_safe = {
            k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
            for k, v in req.tool_args.items()
        }
        print()
        print(f"[审批] 模型请求执行: {req.tool_name}")
        print(f"  参数: {json.dumps(args_safe, ensure_ascii=False, indent=2)}")
        print(f"  approvalId: {req.approval_id}")
        print(f"  输入 /approve {req.approval_id} 批准，或 /reject {req.approval_id} [原因] 拒绝")

        while True:
            try:
                line = input("Approval> ").strip()
            except (EOFError, KeyboardInterrupt):
                approval_manager.reject(req.approval_id, "用户中断")
                return approval_manager.get(req.approval_id) or req

            if not line:
                continue

            if line in EXIT_COMMANDS:
                approval_manager.reject(req.approval_id, "用户退出")
                return approval_manager.get(req.approval_id) or req

            if is_command(line):
                root = line.split()[0]
                if root in ("/approve", "/reject", "/approvals"):
                    result = handle_command(line, state)
                    print(result)
                    decided = approval_manager.get(req.approval_id)
                    if decided and decided.status != "pending":
                        return decided
                else:
                    print("审批等待中，仅支持 /approve、/reject、/approvals、/exit")
            else:
                print("审批等待中，请输入 /approve <id> 或 /reject <id> [原因]")

    print(WELCOME_MESSAGE)
    _print_load_warnings(session_store, memory_store)
    print(f"Current session: {state.current_session_id}")
    if workspace_manager is not None:
        ws = workspace_manager.get(state.current_session_id)
        if ws:
            print(f"Workspace: {ws}")
        else:
            print("Workspace: (未设置) 使用 /workspace set <路径> 设置")
    if skill_registry is not None:
        skills = skill_registry.list_skills()
        print(f"Skills: {len(skills)} loaded ({', '.join(s.name for s in skills)})")
    print()

    while True:
        user_input = _read_user_input()
        if user_input is None:
            break

        if not user_input:
            continue

        if user_input in EXIT_COMMANDS:
            break

        if is_command(user_input):
            print(handle_command(user_input, state))
            print()
            continue

        _handle_chat_turn(
            user_input,
            state,
            client,
            context_builder,
            tool_registry,
            approval_handler=_cli_approval_handler if approval_manager else None,
        )

    # -- Cleanup -----------------------------------------------------------
    if state.compaction_worker is not None:
        print("[compaction] 等待后台压缩任务完成...")
        if not state.compaction_worker.wait(timeout=5.0):
            print("[compaction] 警告：后台压缩未在 5 秒内完成，已放弃等待")
    _print_goodbye()


def _handle_chat_turn(
    user_input: str,
    state: RuntimeState,
    client: LLMClient,
    context_builder: ContextBuilder,
    tool_registry: ToolRegistry,
    *,
    approval_handler=None,
) -> None:
    """Process one user message through the unified agent loop."""
    try:
        reply = run_agent_turn(
            state.current_session_id,
            user_input,
            session_store=state.session_store,
            context_builder=context_builder,
            tool_registry=tool_registry,
            llm_client=client,
            approval_handler=approval_handler,
            compaction_worker=state.compaction_worker,
            auto_mode=state.auto_mode,
            unlimited_mode=(
                state.workspace_manager.is_unlimited(state.current_session_id)
                if state.workspace_manager else False
            ),
        )
    except LLMError as exc:
        _print_error(exc)
        return

    if reply:
        _print_assistant_reply(reply)

    _maybe_auto_title(state, client)
    print()


def _maybe_auto_title(state: RuntimeState, client: LLMClient) -> None:
    """Generate and apply an automatic session title after the first turn."""
    try:
        session = state.session_store.get(state.current_session_id)
    except Exception:
        return

    messages = [{"role": m.role, "content": m.content} for m in session.messages]
    new_title = auto_title_if_first_turn(
        state.current_session_id, messages, state.session_store, client
    )
    if new_title:
        print(f"  [会话标题] {new_title}")


def _print_load_warnings(
    session_store: SessionStore, memory_store: MemoryStore
) -> None:
    for warning in session_store.load_warnings:
        print(f"[警告] {warning}")
    if memory_store.load_warning:
        print(f"[警告] {memory_store.load_warning}")


def _read_user_input() -> str | None:
    try:
        return input("User> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _print_assistant_reply(reply: str) -> None:
    print(f"Assistant> {reply}")


def _print_error(error: Exception) -> None:
    print(f"[错误] {error}")


def _print_goodbye() -> None:
    print("bye.")
