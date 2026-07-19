"""Shared helpers for identifying session-bound cron jobs."""

from __future__ import annotations

from typing import Any

from claw.scheduler.types import CronJob


def is_bound_cron_job(job: CronJob) -> bool:
    """True for session-bound cron jobs with complete delivery context."""
    payload = job.payload
    if (
        payload.kind != "agent_turn"
        or not payload.session_key
        or not payload.origin_channel
        or not payload.origin_chat_id
    ):
        return False
    return not (
        payload.deliver
        or payload.channel
        or payload.to
        or payload.channel_meta
    )


def visible_session_messages(
    session, *, rollback_checkpoint_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """Serialize user-visible messages while hiding Cron execution prompts.

    ``injected_event`` is authoritative for new messages.  The prefix fallback
    also hides scheduler prompts stored by older versions.
    """
    visible: list[dict[str, Any]] = []
    for message in session.messages:
        is_cron_trigger = (
            message.role == "user"
            and (
                message.injected_event == "cron_trigger"
                or message.content.startswith("[定时任务:")
            )
        )
        if not is_cron_trigger:
            serialized = message.to_dict()
            checkpoint_id = serialized.get("rollbackCheckpointId")
            if (
                rollback_checkpoint_ids is not None
                and checkpoint_id not in rollback_checkpoint_ids
            ):
                serialized.pop("rollbackCheckpointId", None)
                serialized.pop("rollbackAvailable", None)
                serialized.pop("messageId", None)
            legacy_prefix = "[定时任务回复]\n\n"
            if message.role == "assistant" and message.content.startswith(legacy_prefix):
                body = message.content[len(legacy_prefix):]
                if (
                    visible
                    and visible[-1].get("role") == "assistant"
                    and visible[-1].get("content") == body
                ):
                    # Older Gateway versions persisted the same reply twice.
                    continue
                serialized["content"] = body
            visible.append(serialized)
    return visible
