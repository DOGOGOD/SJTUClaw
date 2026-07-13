"""Scheduler: cron job scheduling for claw.

Provides the ``CronService`` for managing and executing scheduled jobs,
along with the callback infrastructure for Heartbeat (HEARTBEAT.md monitoring)
system jobs.

Usage::

    from claw.scheduler import CronService
    from claw.scheduler.callbacks import make_heartbeat_system_job

    cron = CronService(store_path)
    cron.register_system_job(make_heartbeat_system_job(cfg))
    cron.start()
"""

from claw.scheduler.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronRunRecord,
    CronSchedule,
    CronStore,
)
from claw.scheduler.service import CronService, _now_ms

__all__ = [
    "CronService",
    "CronJob",
    "CronJobState",
    "CronPayload",
    "CronRunRecord",
    "CronSchedule",
    "CronStore",
    "_now_ms",
]
