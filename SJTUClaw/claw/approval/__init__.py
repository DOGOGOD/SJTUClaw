"""Approval system for write/shell tools (Step 8).

Before executing an update or shell tool, the agent loop must create an
approval request and pause. The user approves or rejects; the result is
fed back into the session history.
"""

from claw.approval.manager import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalStatus,
)

__all__ = ["ApprovalManager", "ApprovalRequest", "ApprovalStatus"]
