"""Small QQ inline-keyboard helpers for interactive approvals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_PREFIX = "sjtuclaw:approval:"


@dataclass(frozen=True)
class QQInteraction:
    interaction_id: str
    chat_type: str
    chat_id: str
    operator_id: str
    button_data: str


def approval_button_data(approval_id: str, decision: str) -> str:
    return f"{_PREFIX}{approval_id}:{decision}"


def parse_approval_button_data(value: str) -> tuple[str, str] | None:
    if not value.startswith(_PREFIX):
        return None
    payload = value[len(_PREFIX):]
    approval_id, separator, decision = payload.rpartition(":")
    if not separator or not approval_id or decision not in {"approve", "reject"}:
        return None
    return approval_id, decision


def build_approval_keyboard(approval_id: str) -> dict[str, Any]:
    def button(button_id: str, label: str, visited: str, decision: str, style: int) -> dict[str, Any]:
        return {
            "id": button_id,
            "render_data": {"label": label, "visited_label": visited, "style": style},
            "action": {
                "type": 1,
                "data": approval_button_data(approval_id, decision),
                "permission": {"type": 2},
                "click_limit": 1,
            },
            "group_id": f"approval-{approval_id}",
        }

    return {
        "content": {
            "rows": [{
                "buttons": [
                    button("approve", "✅ 允许", "已允许", "approve", 1),
                    button("reject", "❌ 拒绝", "已拒绝", "reject", 0),
                ]
            }]
        }
    }


def parse_interaction(raw: dict[str, Any]) -> QQInteraction:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    resolved = data.get("resolved") if isinstance(data.get("resolved"), dict) else {}
    scene_code = int(raw.get("chat_type", 0) or 0)
    chat_type = {0: "dm", 1: "group", 2: "c2c"}.get(scene_code, "")
    if chat_type == "group":
        chat_id = str(raw.get("group_openid", ""))
        operator_id = str(raw.get("group_member_openid", ""))
    elif chat_type == "c2c":
        chat_id = str(raw.get("user_openid", ""))
        operator_id = chat_id
    else:
        chat_id = str(raw.get("guild_id", ""))
        operator_id = str(resolved.get("user_id", ""))
    return QQInteraction(
        interaction_id=str(raw.get("id", "")),
        chat_type=chat_type,
        chat_id=chat_id,
        operator_id=operator_id or str(resolved.get("user_id", "")),
        button_data=str(resolved.get("button_data", "")),
    )
