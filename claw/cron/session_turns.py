"""Shared helpers for identifying session-bound cron jobs."""

from __future__ import annotations

from claw.cron.types import CronJob


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
