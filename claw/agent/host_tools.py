"""Shared host-tool bridge used by external agent backends."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from claw.approval.manager import ApprovalRequest, ApprovalStatus

logger = logging.getLogger(__name__)

NATIVE_TOOL_EQUIVALENTS = frozenset(
    {
        "list_dir",
        "read_file",
        "create_file",
        "overwrite_file",
        "edit_file",
        "new_shell",
        "run_command",
        "skills_list",
        "skill_view",
        "skill_manage",
    }
)

_SAFE_HOST_LEVELS = frozenset({"read_only", "network"})


def list_host_tool_definitions(tool_registry) -> list[dict[str, Any]]:
    """Return SJTUClaw-only tools, excluding native coding equivalents."""
    if tool_registry is None:
        return []
    return [
        definition
        for definition in tool_registry.list_compact_definitions()
        if definition.get("name") not in NATIVE_TOOL_EQUIVALENTS
    ]


def host_tool_requires_approval(tool, args: dict[str, Any] | None = None) -> bool:
    """Return whether a concrete host-tool call can change persistent state."""
    name = str(getattr(tool, "name", "") or "")
    if name == "cron":
        return str((args or {}).get("action") or "").lower() in {"add", "remove"}
    return str(getattr(tool, "safety_level", "") or "") not in _SAFE_HOST_LEVELS


def external_agent_tool_is_preapproved(
    *,
    trust_tools: bool,
    auto_mode: bool,
    unlimited_mode: bool,
) -> bool:
    """Apply one preapproval policy to native and bridged external-agent tools."""
    return bool(trust_tools or (auto_mode and not unlimited_mode))


def execute_host_tool(
    payload: dict[str, Any],
    *,
    session_id: str,
    tool_registry,
    approval_handler,
    trust_tools: bool,
    auto_mode: bool,
    unlimited_mode: bool,
    expected_token: str = "",
) -> dict[str, Any]:
    """Validate, approve when needed, and execute a shared registry tool."""
    supplied_token = str(payload.get("token") or "")
    if expected_token and not hmac.compare_digest(supplied_token, expected_token):
        return {"ok": False, "result": "SJTUClaw 工具桥接认证失败。"}

    name = str(payload.get("toolName") or "")
    args = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    tool = tool_registry.get_tool(name) if tool_registry is not None else None
    if tool is None:
        return {"ok": False, "result": f"未知的 SJTUClaw tool: {name}"}

    mutating = host_tool_requires_approval(tool, args)
    approved = external_agent_tool_is_preapproved(
        trust_tools=trust_tools,
        auto_mode=auto_mode,
        unlimited_mode=unlimited_mode,
    )
    if mutating and not approved:
        if approval_handler is None:
            return {"ok": False, "result": "当前通道不支持审批，操作已拒绝。"}
        request = ApprovalRequest(
            session_id=session_id,
            tool_name=name,
            tool_args=args,
        )
        try:
            decided = approval_handler(request)
            approved = (
                decided is not None
                and decided.status == ApprovalStatus.APPROVED.value
            )
        except Exception:
            logger.exception("外部 Agent 宿主工具审批失败，已安全拒绝")
            approved = False
    if mutating and not approved:
        return {"ok": False, "result": "用户未批准该操作。"}

    result = tool_registry.execute_by_name(name, args, max_result_chars=50_000)
    text = result.content if result.ok else f"错误: {result.error}"
    return {"ok": result.ok, "result": text or "(空结果)"}
