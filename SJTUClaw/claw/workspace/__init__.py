"""Workspace management for claw.

A workspace is the project directory that bounds all file writes,
shell commands, attachment copies and download entry creation.
"""

from claw.workspace.manager import WorkspaceManager, WorkspaceError

__all__ = ["WorkspaceManager", "WorkspaceError"]
