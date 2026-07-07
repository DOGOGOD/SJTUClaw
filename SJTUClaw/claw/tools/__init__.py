"""Tool definitions, registry and built-in tool implementations."""

from claw.tools.base import Tool, ToolRegistry, ToolResult

__all__ = ["Tool", "ToolRegistry", "ToolResult"]


def register_all_tools(
    registry: ToolRegistry,
    *,
    workspace_manager=None,
    session_id_provider=None,
    sessions_dir=None,
    include_skill_tool: bool = False,
) -> None:
    """Register read-only AND advanced tools in *registry*.

    Read-only tools are always registered. Advanced tools (update, shell,
    download, attachment) are only registered when *workspace_manager*
    and *session_id_provider* are provided.

    Args:
        registry: the ``ToolRegistry`` to populate.
        workspace_manager: ``WorkspaceManager`` instance (required for
            write/shell/download/attachment tools).
        session_id_provider: zero-arg callable returning the current
            session id (required for workspace-aware tools).
        sessions_dir: ``Path`` to the sessions directory (required for
            attachment tool).
        include_skill_tool: if True, register the ``use_skill`` tool
            (Step 9).
    """
    from claw.tools.readonly import register_all_readonly

    register_all_readonly(registry)

    # -- Step 9: use_skill tool (always available when requested) ----------
    if include_skill_tool:
        from claw.tools.skills import create_use_skill_tool

        registry.register(create_use_skill_tool())

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
