"""Scheduler types — cron job definitions.

v2 enhancements:

- **Task dependencies**: ``depends_on`` lists job IDs whose most recent
  output is injected as context before this job runs.
- **Pause reasons**: ``paused_at_ms`` / ``paused_reason`` track why a
  job was paused for operational visibility.
- **Claim-based dispatch**: ``run_claim`` / ``fire_claim`` support
  at-most-once execution semantics across crash/restart cycles.
- **Output persistence**: each execution's output is saved to a file
  under ``runs/<job_id>/<timestamp>.md`` for dependency injection and
  audit.
- **Heartbeat liveness**: ``heartbeat_at_ms`` / ``last_success_at_ms``
  let external monitors detect a dead ticker thread.
"""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    # Legacy delivery fields used by pre-session-bound cron jobs.
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    channel_meta: dict[str, Any] = field(default_factory=dict)
    session_key: str | None = None  # original session key for correct session recording
    origin_channel: str | None = None
    origin_chat_id: str | None = None
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    # v2: job IDs whose latest output is injected as context before run.
    depends_on: list[str] = field(default_factory=list)


@dataclass
class CronRunRecord:
    """A single execution record for a cron job."""
    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None
    # v2: path to the saved output file (empty if not persisted).
    output_path: str = ""


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)
    # v2: pause tracking for operational visibility.
    paused_at_ms: int | None = None
    paused_reason: str = ""
    # v2: at-most-once claim for crash-safe dispatch.
    run_claim: dict[str, Any] | None = None
    fire_claim: dict[str, Any] | None = None


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False
    # v2: repeat limit (None = forever, N = run N times then auto-delete).
    repeat_times: int | None = None
    repeat_completed: int = 0

    @classmethod
    def from_dict(cls, kwargs: dict) -> "CronJob":
        state_kwargs = dict(kwargs.get("state", {}))
        state_kwargs["run_history"] = [
            record if isinstance(record, CronRunRecord) else CronRunRecord(**record)
            for record in state_kwargs.get("run_history", [])
        ]
        kwargs["schedule"] = CronSchedule(**kwargs.get("schedule", {"kind": "every"}))
        kwargs["payload"] = CronPayload(**kwargs.get("payload", {}))
        kwargs["state"] = CronJobState(**state_kwargs)
        return cls(**kwargs)


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
    # v2: heartbeat liveness signals for external monitors.
    heartbeat_at_ms: int = 0
    last_success_at_ms: int = 0
