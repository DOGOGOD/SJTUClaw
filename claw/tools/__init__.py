"""Tool definitions, registry and built-in tool implementations."""

from claw.tools.base import (
    Tool,
    ToolGuardrails,
    ToolRegistry,
    ToolResult,
    normalize_tool_schema,
    standardize_tool_result,
)

__all__ = [
    "Tool",
    "ToolGuardrails",
    "ToolRegistry",
    "ToolResult",
    "normalize_tool_schema",
    "standardize_tool_result",
]


def register_all_tools(
    registry: ToolRegistry,
    *,
    workspace_manager=None,
    session_id_provider=None,
    sessions_dir=None,
    include_skill_tool: bool = False,
    skill_registry=None,
    include_memory_tools: bool = False,
    memory_store=None,
    include_cron_tool: bool = False,
    cron_service=None,
    default_timezone: str = "UTC",
) -> None:
    """Register read-only AND advanced tools in *registry*.

    Read-only tools are always registered. Advanced tools (update, shell,
    download, attachment, memory) are only registered when their
    dependencies are provided.

    Args:
        registry: the ``ToolRegistry`` to populate.
        workspace_manager: ``WorkspaceManager`` instance (required for
            write/shell/download/attachment tools).
        session_id_provider: zero-arg callable returning the current
            session id (required for workspace-aware tools).
        sessions_dir: ``Path`` to the sessions directory (required for
            attachment tool).
        include_skill_tool: if True, register ``skills_list`` and
            ``skill_view`` tools (Step 9).
        include_memory_tools: if True, register ``remember`` and
            ``recall`` tools (hierarchical memory).
        memory_store: ``MemoryStore`` instance (required for memory tools).
    """
    from claw.tools.readonly import register_all_readonly
    from claw.tools.web import register_web_tools

    register_all_readonly(registry, workspace_manager, session_id_provider)
    # Network tools are read-only and do not depend on a workspace.  They are
    # enabled by default and can be disabled with WEB_TOOL_ENABLED=false.
    register_web_tools(registry)

    # -- Step 9: skills_list + skill_view + skill_manage tools ------------
    if include_skill_tool and skill_registry is not None:
        from claw.tools.skills_tool import create_skills_list_tool, create_skill_view_tool
        from claw.tools.skill_manager_tool import create_skill_manage_tool

        registry.register(create_skills_list_tool(skill_registry))
        registry.register(create_skill_view_tool(skill_registry))
        registry.register(create_skill_manage_tool(skill_registry))

    # -- Hierarchical memory tools ----------------------------------------
    if include_memory_tools and memory_store is not None:
        from claw.tools.memory_tools import create_recall_tool, create_remember_tool

        registry.register(create_remember_tool(memory_store, session_id_provider))
        registry.register(create_recall_tool(memory_store))

    # -- Cron tool ----------------------------------------------------------
    if include_cron_tool and cron_service is not None:
        from claw.tools.cron_tool import CronTool

        cron_tool = CronTool(cron_service, default_timezone)
        if session_id_provider is not None:
            # Set context if session provider is available
            try:
                sid = session_id_provider()
                cron_tool.set_context(session_key=sid, channel="cli", chat_id=sid)
            except Exception:
                pass
        registry.register(cron_tool)

    if workspace_manager is None or session_id_provider is None:
        return

    from claw.tools.update import (
        create_create_file_tool,
        create_edit_file_tool,
        create_overwrite_file_tool,
    )
    from claw.tools.shell import create_new_shell_tool, create_run_command_tool
    from claw.tools.download import create_download_tool
    from claw.tools.attachment import create_copy_attachment_tool

    registry.register(
        create_create_file_tool(workspace_manager, session_id_provider)
    )
    registry.register(
        create_overwrite_file_tool(workspace_manager, session_id_provider)
    )
    registry.register(
        create_edit_file_tool(workspace_manager, session_id_provider)
    )
    registry.register(
        create_new_shell_tool(workspace_manager, session_id_provider)
    )
    registry.register(
        create_run_command_tool(workspace_manager, session_id_provider)
    )
    registry.register(
        create_download_tool(workspace_manager, session_id_provider)
    )
    if sessions_dir is not None:
        registry.register(
            create_copy_attachment_tool(
                workspace_manager, session_id_provider, sessions_dir
            )
        )
