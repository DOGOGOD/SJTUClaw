"""Cron service for scheduling agent tasks.



Moved from claw.cron to claw.scheduler.

"""



from __future__ import annotations



import asyncio

import errno

import json

import logging

import os

import threading

import time

import uuid

from contextlib import suppress

from dataclasses import asdict

from datetime import datetime

from pathlib import Path

from typing import Any, Callable, Coroutine, Literal



from filelock import FileLock



from claw.scheduler.session_turns import is_bound_cron_job

from claw.scheduler.types import (

    CronJob,

    CronJobState,

    CronPayload,

    CronRunRecord,

    CronSchedule,

    CronStore,

)



logger = logging.getLogger(__name__)





class CronJobSkippedError(Exception):

    """Raised by cron callbacks when a job was intentionally skipped."""





def _now_ms() -> int:

    return int(time.time() * 1000)





def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:

    """Compute next run time in ms."""

    if schedule.kind == "at":

        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None



    if schedule.kind == "every":

        if not schedule.every_ms or schedule.every_ms <= 0:

            return None

        # Next interval from now

        return now_ms + schedule.every_ms



    if schedule.kind == "cron" and schedule.expr:

        try:

            from zoneinfo import ZoneInfo



            from croniter import croniter

            # Use caller-provided reference time for deterministic scheduling

            base_time = now_ms / 1000

            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo

            base_dt = datetime.fromtimestamp(base_time, tz=tz)

            cron = croniter(schedule.expr, base_dt)

            next_dt = cron.get_next(datetime)

            return int(next_dt.timestamp() * 1000)

        except Exception:

            return None



    return None





def _validate_schedule_for_add(schedule: CronSchedule) -> None:

    """Validate schedule fields that would otherwise create non-runnable jobs."""

    if schedule.kind == "at":

        if not isinstance(schedule.at_ms, int) or schedule.at_ms <= _now_ms():

            raise ValueError("at schedule must use a future at_ms timestamp")

        if schedule.every_ms is not None or schedule.expr is not None or schedule.tz:

            raise ValueError("at schedule cannot include every_ms, expr, or tz")

        return



    if schedule.kind == "every":

        if not isinstance(schedule.every_ms, int) or schedule.every_ms <= 0:

            raise ValueError("every schedule requires every_ms > 0")

        if schedule.at_ms is not None or schedule.expr is not None or schedule.tz:

            raise ValueError("every schedule cannot include at_ms, expr, or tz")

        return



    if schedule.kind != "cron":

        raise ValueError(f"unsupported cron schedule kind: {schedule.kind!r}")

    if not isinstance(schedule.expr, str) or not schedule.expr.strip():

        raise ValueError("cron schedule requires a non-empty expr")



    try:

        from zoneinfo import ZoneInfo

        from croniter import croniter



        tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo

        croniter(schedule.expr, datetime.now(tz=tz)).get_next(datetime)

    except Exception as exc:

        if schedule.tz:

            try:

                ZoneInfo(schedule.tz)

            except Exception:

                raise ValueError(f"unknown timezone '{schedule.tz}'") from None

        raise ValueError(f"invalid cron expression {schedule.expr!r}: {exc}") from None





def _has_legacy_delivery_context(payload: CronPayload) -> bool:

    return bool(payload.deliver or payload.channel or payload.to or payload.channel_meta)





def _legacy_session_key(payload: CronPayload) -> str | None:

    if payload.session_key:

        return payload.session_key

    if payload.channel and payload.to:

        return f"{payload.channel}:{payload.to}"

    return None





def _disable_malformed_legacy_job(job: CronJob) -> None:

    reason = "legacy cron payload is missing channel/to; recreate it from a chat session"

    job.payload.deliver = False

    job.payload.channel = None

    job.payload.to = None

    job.payload.channel_meta = {}

    job.enabled = False

    job.state.next_run_at_ms = None

    job.state.last_status = "error"

    job.state.last_error = reason

    logger.warning("Cron: disabled malformed legacy job '%s' (%s): %s", job.name, job.id, reason)





def _normalize_agent_turn_job(job: CronJob) -> bool:

    """Migrate legacy user cron payloads into session-bound payloads.



    Pre-bound user cron jobs stored their delivery target in ``channel``/``to``.

    Normal user-created legacy jobs always have those fields; if they are

    missing, keep the record for inspection but disable it instead of preserving

    a runtime legacy execution path.

    """

    payload = job.payload

    if payload.kind != "agent_turn" or not _has_legacy_delivery_context(payload):

        return False



    if not payload.channel or not payload.to:

        _disable_malformed_legacy_job(job)

        return True



    payload.session_key = _legacy_session_key(payload)

    payload.origin_channel = payload.origin_channel or payload.channel

    payload.origin_chat_id = payload.origin_chat_id or payload.to

    if not payload.origin_metadata:

        payload.origin_metadata = dict(payload.channel_meta or {})



    payload.deliver = False

    payload.channel = None

    payload.to = None

    payload.channel_meta = {}

    job.updated_at_ms = max(job.updated_at_ms, _now_ms())

    logger.info("Cron: migrated legacy job '%s' (%s) to session-bound payload", job.name, job.id)

    return True





class CronService:

    """Service for managing and executing scheduled jobs.



    Usage::



        srv = CronService(store_path, on_job=my_callback)

        srv.start()          # start background timer

        ...

        srv.stop()

    """



    _MAX_RUN_HISTORY = 20

    _UNBOUND_AGENT_JOB_REASON = (

        "agent cron payload is missing bound session delivery context; "

        "recreate it from a chat session"

    )

    # v2: one-shot run_claim TTL — safety valve for stale claims left by

    # a tick that died mid-run. Derived from the cron inactivity timeout.

    _RUN_CLAIM_TTL_S = 1800  # 30 minutes

    # v2: max output files retained per job (prevents disk fill).

    _OUTPUT_RETENTION = 50



    def __init__(

        self,

        store_path: Path | str,

        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,

        max_sleep_ms: int = 300_000,  # 5 minutes

    ):

        self.store_path = Path(store_path)

        self._action_path = self.store_path.parent / "action.jsonl"

        self._run_records_dir = self.store_path.parent / "runs"

        self._lock = FileLock(str(self._action_path.parent) + ".lock")

        self.on_job = on_job

        self._store: CronStore | None = None

        self._timer_task: asyncio.Task | None = None

        self._running = False

        self._timer_active = False

        self.max_sleep_ms = max_sleep_ms

        self._loop: asyncio.AbstractEventLoop | None = None

        self._loop_thread: threading.Thread | None = None



    def _is_unbound_agent_job(self, job: CronJob) -> bool:

        return job.payload.kind == "agent_turn" and not is_bound_cron_job(job)



    def _enforce_agent_binding(self, job: CronJob) -> bool:

        """Disable user cron jobs that cannot be routed to a concrete session."""

        if not self._is_unbound_agent_job(job):

            return False

        if (

            not job.enabled

            and job.state.next_run_at_ms is None

            and job.state.last_status == "error"

            and job.state.last_error

        ):

            return False



        job.enabled = False

        job.state.next_run_at_ms = None

        job.state.last_status = "error"

        job.state.last_error = self._UNBOUND_AGENT_JOB_REASON

        job.updated_at_ms = max(job.updated_at_ms, _now_ms())

        logger.warning(

            "Cron: disabled unbound agent job '%s' (%s): %s",

            job.name,

            job.id,

            self._UNBOUND_AGENT_JOB_REASON,

        )

        return True



    def _enforce_store_agent_bindings(self) -> bool:

        if not self._store:

            return False

        changed = False

        for job in self._store.jobs:

            changed = self._enforce_agent_binding(job) or changed

        return changed



    # ------------------------------------------------------------------

    # Lifecycle

    # ------------------------------------------------------------------



    def start(self, *, loop: asyncio.AbstractEventLoop | None = None) -> None:

        """Start the cron service.



        If *loop* is provided (e.g. from FastAPI lifespan), the cron timer

        runs on that loop.  Otherwise a background daemon thread is

        spawned with its own event loop.

        """

        if self._running:

            logger.debug("Cron service is already running; ignoring duplicate start")

            return



        self._running = True



        if loop is not None:

            self._loop = loop

            self._start_on_loop()

            return



        # Background thread with its own event loop

        self._loop_thread = threading.Thread(

            target=self._run_loop_in_thread,

            daemon=True,

            name="claw-cron",

        )

        self._loop_thread.start()



    def _run_loop_in_thread(self) -> None:

        """Run the asyncio event loop in a background thread."""

        self._loop = asyncio.new_event_loop()

        asyncio.set_event_loop(self._loop)

        try:

            self._start_on_loop()

            self._loop.run_forever()

        finally:

            self._loop.close()



    def _start_on_loop(self) -> None:

        """Schedule the initial cron tick on the active event loop."""

        assert self._loop is not None

        asyncio.run_coroutine_threadsafe(self._start(), self._loop)



    async def _start(self) -> None:

        """Internal async start — load store, recompute, arm timer."""

        loaded = self._load_store()

        if loaded is None:

            # Store file existed but was corrupt and has been preserved with

            # a ``.corrupt-<ts>`` suffix.  Bail out instead of starting with

            # an empty store; that would call ``_save_store`` and overwrite

            # the now-renamed (but still recoverable) data with [].

            self._running = False

            raise RuntimeError(

                f"cron store at {self.store_path} is corrupt and was preserved; "

                "refusing to start with an empty job list. "

                "Inspect the .corrupt-<ts> backup and restore manually."

            )

        self._recompute_next_runs()

        self._save_store()

        self._arm_timer()

        logger.info("Cron service started with %s jobs", len(self._store.jobs if self._store else []))



    def stop(self) -> None:

        """Stop the cron service."""

        self._running = False

        if self._timer_task:

            self._timer_task.cancel()

            self._timer_task = None

        if self._loop_thread and self._loop_thread.is_alive():

            if self._loop:

                self._loop.call_soon_threadsafe(self._loop.stop)

            self._loop_thread.join(timeout=5)



    # ------------------------------------------------------------------

    # Store persistence

    # ------------------------------------------------------------------



    def _load_jobs(self) -> tuple[list[CronJob], int] | None:

        """Load jobs from disk.



        Returns:

            ``(jobs, version)`` tuple on success or when no store file exists

            (in which case an empty list and version 1 are returned).

            ``None`` when the store file exists but cannot be parsed; the

            corrupt file is preserved with a ``.corrupt-<ts>`` suffix so the

            caller can decide whether to overwrite or bail out.  Returning a

            sentinel here is important: silently treating a parse error as an

            empty job list would cause the next ``_save_store`` to wipe every

            job from disk.

        """

        jobs: list[CronJob] = []

        version = 1

        if self.store_path.exists():

            try:

                data = json.loads(self.store_path.read_text(encoding="utf-8"))

                jobs = []

                version = data.get("version", 1)

                for j in data.get("jobs", []):

                    state_data = j.get("state", {})

                    payload_data = j.get("payload", {})

                    job = CronJob(

                        id=j["id"],

                        name=j["name"],

                        enabled=j.get("enabled", True),

                        schedule=CronSchedule(

                            kind=j["schedule"]["kind"],

                            at_ms=j["schedule"].get("atMs"),

                            every_ms=j["schedule"].get("everyMs"),

                            expr=j["schedule"].get("expr"),

                            tz=j["schedule"].get("tz"),

                        ),

                        payload=CronPayload(

                            kind=payload_data.get("kind", "agent_turn"),

                            message=payload_data.get("message", ""),

                            deliver=payload_data.get("deliver", False),

                            channel=payload_data.get("channel"),

                            to=payload_data.get("to"),

                            channel_meta=(

                                payload_data.get("channelMeta")

                                or payload_data.get("channel_meta")

                                or {}

                            ),

                            session_key=payload_data.get("sessionKey") or payload_data.get("session_key"),

                            origin_channel=(

                                payload_data.get("originChannel")

                                or payload_data.get("origin_channel")

                            ),

                            origin_chat_id=(

                                payload_data.get("originChatId")

                                or payload_data.get("origin_chat_id")

                            ),

                            origin_metadata=(

                                payload_data.get("originMetadata")

                                or payload_data.get("origin_metadata")

                                or {}

                            ),

                            depends_on=payload_data.get("dependsOn", []),

                        ),

                        state=CronJobState(

                            next_run_at_ms=state_data.get("nextRunAtMs"),

                            last_run_at_ms=state_data.get("lastRunAtMs"),

                            last_status=state_data.get("lastStatus"),

                            last_error=state_data.get("lastError"),

                            run_history=[

                                CronRunRecord(

                                    run_at_ms=r["runAtMs"],

                                    status=r["status"],

                                    duration_ms=r.get("durationMs", 0),

                                    error=r.get("error"),

                                    output_path=r.get("outputPath", ""),

                                )

                                for r in state_data.get("runHistory", [])

                            ],

                            paused_at_ms=state_data.get("pausedAtMs"),

                            paused_reason=state_data.get("pausedReason", ""),

                            run_claim=state_data.get("runClaim"),

                            fire_claim=state_data.get("fireClaim"),

                        ),

                        created_at_ms=j.get("createdAtMs", 0),

                        updated_at_ms=j.get("updatedAtMs", 0),

                        delete_after_run=j.get("deleteAfterRun", False),

                        repeat_times=j.get("repeatTimes"),

                        repeat_completed=j.get("repeatCompleted", 0),

                    )

                    _normalize_agent_turn_job(job)

                    jobs.append(job)

            except Exception:

                # Preserve the corrupt file for forensic recovery instead of

                # letting the next save overwrite it with an empty job list.

                backup = self.store_path.with_suffix(

                    self.store_path.suffix + f".corrupt-{int(time.time())}"

                )

                with suppress(OSError):

                    self.store_path.rename(backup)

                logger.exception(

                    "Failed to load cron store at %s. "

                    "Corrupt file preserved at %s. "

                    "Refusing to overwrite to avoid data loss.",

                    self.store_path,

                    backup,

                )

                return None

        return jobs, version



    def _merge_action(self) -> None:

        if not self._action_path.exists():

            return



        if not self._store:

            return



        jobs_map = {j.id: j for j in self._store.jobs}



        def _update(params: dict):

            j = CronJob.from_dict(params)

            _normalize_agent_turn_job(j)

            jobs_map[j.id] = j



        def _del(params: dict):

            if job_id := params.get("job_id"):

                jobs_map.pop(job_id, None)



        with self._lock:

            with open(self._action_path, "r", encoding="utf-8") as f:

                changed = False

                for line in f:

                    try:

                        line = line.strip()

                        action = json.loads(line)

                        if "action" not in action:

                            continue

                        if action["action"] == "del":

                            _del(action.get("params", {}))

                        else:

                            _update(action.get("params", {}))

                        changed = True

                    except Exception:

                        logger.exception("load action line error")

                        continue

            self._store.jobs = list(jobs_map.values())

            if self._running and changed:

                self._action_path.write_text("", encoding="utf-8")

                self._save_store()



    def _load_store(self) -> CronStore | None:

        """Load jobs from disk. Reloads automatically if file was modified externally.

        - Reload every time because it needs to merge operations on the jobs object from other instances.

        - During _on_timer execution, return the existing store to prevent concurrent

          _load_store calls (e.g. from list_jobs polling) from replacing it mid-execution.

        - When the on-disk store exists but is unreadable: keep using the

          previous in-memory ``self._store`` if we already have one (so a

          transient corruption does not drop live jobs); only the very first

          load (during ``start``) can return ``None`` to signal an unrecoverable

          state to the caller.

        """

        if self._timer_active and self._store:

            return self._store

        loaded = self._load_jobs()

        if loaded is None:

            # Corrupt store on disk.  Prefer the last good in-memory snapshot

            # over wiping live jobs; ``_load_jobs`` has already moved the

            # corrupt file aside with a ``.corrupt-<ts>`` suffix.

            if self._store is not None:

                return self._store

            return None

        jobs, version = loaded

        self._store = CronStore(version=version, jobs=jobs)

        # Restore heartbeat fields from the raw data

        if self.store_path.exists():

            try:

                raw = json.loads(self.store_path.read_text(encoding="utf-8"))

                self._store.heartbeat_at_ms = raw.get("heartbeatAtMs", 0)

                self._store.last_success_at_ms = raw.get("lastSuccessAtMs", 0)

            except Exception:

                pass

        self._merge_action()

        if self._enforce_store_agent_bindings() and self._running:

            self._save_store()



        return self._store



    def _require_store(self) -> CronStore:

        """Return a usable store or raise a clear error.



        ``_load_store`` deliberately returns ``None`` when the first load sees

        a corrupt on-disk store and no previous in-memory snapshot exists.  The

        public API requires a concrete store object before touching

        ``store.jobs``; raising here keeps callers from seeing an accidental

        ``AttributeError`` and, more importantly, prevents follow-up saves from

        treating a corrupt store as an empty one.

        """

        store = self._load_store()

        if store is None:

            raise RuntimeError(

                f"cron store at {self.store_path} could not be loaded and was preserved "

                "as a .corrupt-<ts> backup; refusing to operate to avoid overwriting "

                "scheduled jobs. Inspect the corrupt backup and restore jobs.json manually."

            )

        return store



    def _save_store(self) -> None:

        """Save jobs to disk."""

        if not self._store:

            return



        self.store_path.parent.mkdir(parents=True, exist_ok=True)



        data = {

            "version": self._store.version,

            "heartbeatAtMs": self._store.heartbeat_at_ms,

            "lastSuccessAtMs": self._store.last_success_at_ms,

            "jobs": [

                {

                    "id": j.id,

                    "name": j.name,

                    "enabled": j.enabled,

                    "schedule": {

                        "kind": j.schedule.kind,

                        "atMs": j.schedule.at_ms,

                        "everyMs": j.schedule.every_ms,

                        "expr": j.schedule.expr,

                        "tz": j.schedule.tz,

                    },

                    "payload": {

                        "kind": j.payload.kind,

                        "message": j.payload.message,

                        "deliver": j.payload.deliver,

                        "channel": j.payload.channel,

                        "to": j.payload.to,

                        "channelMeta": j.payload.channel_meta,

                        "sessionKey": j.payload.session_key,

                        "originChannel": j.payload.origin_channel,

                        "originChatId": j.payload.origin_chat_id,

                        "originMetadata": j.payload.origin_metadata,

                        "dependsOn": j.payload.depends_on,

                    },

                    "state": {

                        "nextRunAtMs": j.state.next_run_at_ms,

                        "lastRunAtMs": j.state.last_run_at_ms,

                        "lastStatus": j.state.last_status,

                        "lastError": j.state.last_error,

                        "runHistory": [

                            {

                                "runAtMs": r.run_at_ms,

                                "status": r.status,

                                "durationMs": r.duration_ms,

                                "error": r.error,

                                "outputPath": r.output_path,

                            }

                            for r in j.state.run_history

                        ],

                        "pausedAtMs": j.state.paused_at_ms,

                        "pausedReason": j.state.paused_reason,

                        "runClaim": j.state.run_claim,

                        "fireClaim": j.state.fire_claim,

                    },

                    "createdAtMs": j.created_at_ms,

                    "updatedAtMs": j.updated_at_ms,

                    "deleteAfterRun": j.delete_after_run,

                    "repeatTimes": j.repeat_times,

                    "repeatCompleted": j.repeat_completed,

                }

                for j in self._store.jobs

            ],

        }



        self._atomic_write(self.store_path, json.dumps(data, indent=2, ensure_ascii=False))



    @staticmethod

    def _atomic_write(path: Path, content: str) -> None:

        """Write *content* to *path* atomically with fsync.



        Uses a temp-file + ``os.replace`` + ``fsync`` pattern so a crash or

        SIGKILL mid-write cannot leave the destination truncated or invalid.

        Without this, ``jobs.json`` could be corrupted on container shutdown

        and silently re-created empty on next start, wiping every scheduled job.

        """

        path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = path.with_suffix(path.suffix + ".tmp")

        try:

            with open(tmp_path, "w", encoding="utf-8") as f:

                f.write(content)

                f.flush()

                os.fsync(f.fileno())

            # Windows 上 os.replace 可能因目标文件被其他进程短暂占用而
            # 抛出 PermissionError [WinError 5]，重试几次即可成功。
            last_exc: Exception | None = None
            for _attempt in range(6):
                try:
                    os.replace(tmp_path, path)
                    last_exc = None
                    break
                except PermissionError as exc:
                    last_exc = exc
                    time.sleep(0.05)
            if last_exc is not None:
                raise last_exc

            # fsync the parent directory so the rename itself is durable.

            # Skip on Windows where opening a directory raises PermissionError;

            # some shared filesystems reject directory fsync with EINVAL.

            with suppress(PermissionError):

                fd = os.open(str(path.parent), os.O_RDONLY)

                try:

                    try:

                        os.fsync(fd)

                    except OSError as exc:

                        if exc.errno != errno.EINVAL:

                            raise

                finally:

                    os.close(fd)

        except BaseException:

            tmp_path.unlink(missing_ok=True)

            raise



    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:

        """Write an internal audit record for one cron execution."""

        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in run_id) or str(uuid.uuid4())

        path = self._run_records_dir / f"{name}.json"

        payload = {

            **record,

            "run_id": run_id,

            "updated_at_ms": _now_ms(),

        }

        self._atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False))



    # ------------------------------------------------------------------

    # Timer / execution

    # ------------------------------------------------------------------



    def _recompute_next_runs(self) -> None:

        """Recompute next run times for all enabled jobs."""

        if not self._store:

            return

        now = _now_ms()

        for job in self._store.jobs:

            if self._enforce_agent_binding(job):

                continue

            if job.enabled:

                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)



    def _get_next_wake_ms(self) -> int | None:

        """Get the earliest next run time across all jobs."""

        if not self._store:

            return None

        times = [j.state.next_run_at_ms for j in self._store.jobs

                 if j.enabled and j.state.next_run_at_ms]

        return min(times) if times else None



    def _arm_timer(self) -> None:

        """Schedule the next timer tick, thread-safely.



        Public API (add_job, enable_job, …) may call this from any thread.

        The timer must always run on ``self._loop``, so we detect whether

        we are already on that loop and schedule accordingly.

        """

        if not self._running or not self._loop or not self._loop.is_running():

            return



        next_wake = self._get_next_wake_ms()

        if next_wake is None:

            delay_ms = self.max_sleep_ms

        else:

            delay_ms = min(self.max_sleep_ms, max(0, next_wake - _now_ms()))

        delay_s = delay_ms / 1000



        def _create_timer() -> None:

            if self._timer_task:

                self._timer_task.cancel()

                self._timer_task = None



            async def tick():

                await asyncio.sleep(delay_s)

                if self._running:

                    await self._on_timer()



            self._timer_task = asyncio.ensure_future(tick())



        try:

            loop = asyncio.get_running_loop()

            if loop is self._loop:

                # Already on the cron event loop — just create the task

                _create_timer()

            else:

                self._loop.call_soon_threadsafe(_create_timer)

        except RuntimeError:

            # No running event loop in the calling thread

            self._loop.call_soon_threadsafe(_create_timer)



    async def _on_timer(self) -> None:

        """Handle timer tick - run due jobs."""

        self._load_store()

        # If a hot reload found a corrupt store on disk, ``self._store`` may

        # still hold the previous, known-good in-memory snapshot.  Keep using

        # it rather than crashing the timer or wiping live jobs.

        if not self._store:

            self._arm_timer()

            return



        self._timer_active = True

        try:

            # v2: record heartbeat at the start of each tick

            self._record_heartbeat(success=False)



            now = _now_ms()

            due_jobs = [

                j for j in self._store.jobs

                if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms

            ]



            for job in due_jobs:

                # v2: skip one-shot jobs with an active run_claim (at-most-once)

                if job.schedule.kind == "at" and job.state.run_claim:

                    claim_age_s = (now - job.state.run_claim.get("at_ms", 0)) / 1000

                    if 0 <= claim_age_s < self._RUN_CLAIM_TTL_S:

                        logger.info(

                            "Cron: skipping job '%s' — active run_claim (%.0fs old)",

                            job.name, claim_age_s,

                        )

                        continue

                # v2: advance recurring job next_run before execution (crash safety)

                if job.schedule.kind in ("cron", "every"):

                    self._advance_next_run(job)

                await self._execute_job(job)



            # v2: heartbeat with success=True if we got here without raising

            self._record_heartbeat(success=True)

            self._save_store()

        except asyncio.CancelledError:

            raise

        except Exception:

            # A single bad tick must not permanently stop the scheduler.

            logger.exception("Cron timer tick failed; scheduling the next tick")

        finally:

            self._timer_active = False

            self._arm_timer()



    async def _execute_job(self, job: CronJob) -> None:

        """Execute a single job.



        v2 enhancements:

        - Injects dependency context (depends_on outputs) into the payload

          message before execution.

        - Saves execution output to a file for audit and dependency injection.

        - Handles repeat limits (repeat_times / repeat_completed).

        """

        start_ms = _now_ms()

        logger.info("Cron: executing job '%s' (%s)", job.name, job.id)



        # v2: inject dependency context

        original_message = job.payload.message

        dep_context = self._build_dependency_context(job)

        if dep_context:

            job.payload.message = (

                f"{original_message}\n\n"

                f"[依赖任务上下文]\n{dep_context}"

            )



        # v2: stamp run_claim for one-shot jobs (at-most-once dispatch)

        if job.schedule.kind == "at":

            job.state.run_claim = {"at_ms": start_ms, "by": self._machine_id()}



        try:

            if self.on_job:

                result_text = await self.on_job(job)

            else:

                result_text = None



            job.state.last_status = "ok"

            job.state.last_error = None

            logger.info("Cron: job '%s' completed", job.name)



        except CronJobSkippedError as e:

            job.state.last_status = "skipped"

            job.state.last_error = str(e) or None

            result_text = None

            logger.warning("Cron: job '%s' skipped: %s", job.name, job.state.last_error or "")

        except asyncio.CancelledError as e:

            current = asyncio.current_task()

            if current is not None and current.cancelling():

                raise

            job.state.last_status = "error"

            job.state.last_error = str(e) or e.__class__.__name__

            result_text = None

            logger.exception("Cron: job '%s' was cancelled", job.name)

        except Exception as e:

            job.state.last_status = "error"

            job.state.last_error = str(e)

            result_text = None

            logger.exception("Cron: job '%s' failed", job.name)



        # v2: restore original message (don't persist the injected context)

        job.payload.message = original_message



        # v2: clear the run_claim (execution is done)

        job.state.run_claim = None



        end_ms = _now_ms()

        job.state.last_run_at_ms = start_ms

        job.updated_at_ms = end_ms



        # v2: save execution output

        output_path = ""

        if result_text and job.payload.kind != "system_event":

            output_path = self._save_job_output(job.id, result_text, start_ms)



        job.state.run_history.append(CronRunRecord(

            run_at_ms=start_ms,

            status=job.state.last_status,

            duration_ms=end_ms - start_ms,

            error=job.state.last_error,

            output_path=output_path,

        ))

        job.state.run_history = job.state.run_history[-self._MAX_RUN_HISTORY:]



        # v2: handle repeat limits

        if job.repeat_times is not None and job.repeat_times > 0:

            job.repeat_completed += 1

            if job.repeat_completed >= job.repeat_times:

                # Limit reached — remove the job

                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]

                logger.info("Cron: job '%s' reached repeat limit (%d/%d), removed",

                            job.name, job.repeat_completed, job.repeat_times)

                return



        # Handle one-shot jobs

        if job.schedule.kind == "at":

            if job.delete_after_run:

                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]

            else:

                job.enabled = False

                job.state.next_run_at_ms = None

        else:

            # Compute next run (if not already advanced by _advance_next_run)

            if job.state.next_run_at_ms is None or job.state.next_run_at_ms <= end_ms:

                job.state.next_run_at_ms = _compute_next_run(job.schedule, end_ms)



    def _append_action(self, action: Literal["add", "del", "update"], params: dict) -> None:

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:

            with open(self._action_path, "a", encoding="utf-8") as f:

                f.write(json.dumps({"action": action, "params": params}, ensure_ascii=False) + "\n")



    # ========== Public API ==========



    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:

        """List all jobs."""

        store = self._require_store()

        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]

        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))



    def list_bound_cron_jobs_for_session(

        self,

        session_key: str,

        *,

        include_disabled: bool = True,

    ) -> list[CronJob]:

        """Return user-created bound cron jobs owned by *session_key*."""

        return [

            job

            for job in self.list_jobs(include_disabled=include_disabled)

            if is_bound_cron_job(job)

            and job.payload.session_key == session_key

        ]



    def add_job(

        self,

        name: str,

        schedule: CronSchedule,

        message: str,

        deliver: bool = False,

        channel: str | None = None,

        to: str | None = None,

        delete_after_run: bool = False,

        channel_meta: dict | None = None,

        session_key: str | None = None,

        origin_channel: str | None = None,

        origin_chat_id: str | None = None,

        origin_metadata: dict | None = None,

        depends_on: list[str] | None = None,

        repeat_times: int | None = None,

    ) -> CronJob:

        """Add a new job.



        v2 parameters:

            depends_on: list of job IDs whose latest output is injected

                as context before this job runs (task dependency chain).

            repeat_times: if set, the job auto-deletes after this many

                executions. None means run forever.

        """

        _validate_schedule_for_add(schedule)

        now = _now_ms()



        job = CronJob(

            id=str(uuid.uuid4())[:8],

            name=name,

            enabled=True,

            schedule=schedule,

            payload=CronPayload(

                kind="agent_turn",

                message=message,

                deliver=deliver,

                channel=channel,

                to=to,

                channel_meta=channel_meta or {},

                session_key=session_key,

                origin_channel=origin_channel,

                origin_chat_id=origin_chat_id,

                origin_metadata=origin_metadata or {},

                depends_on=list(depends_on) if depends_on else [],

            ),

            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),

            created_at_ms=now,

            updated_at_ms=now,

            delete_after_run=delete_after_run,

            repeat_times=repeat_times,

            repeat_completed=0,

        )

        _normalize_agent_turn_job(job)

        self._enforce_agent_binding(job)

        if self._running:

            store = self._require_store()

            store.jobs.append(job)

            self._save_store()

            self._arm_timer()

        else:

            self._append_action("add", asdict(job))



        logger.info("Cron: added job '%s' (%s)", name, job.id)

        return job



    def register_system_job(self, job: CronJob) -> CronJob:

        """Register an internal system job (idempotent on restart)."""

        store = self._require_store()

        now = _now_ms()

        job.state = CronJobState(next_run_at_ms=_compute_next_run(job.schedule, now))

        job.created_at_ms = now

        job.updated_at_ms = now

        store.jobs = [j for j in store.jobs if j.id != job.id]

        store.jobs.append(job)

        self._save_store()

        self._arm_timer()

        logger.info("Cron: registered system job '%s' (%s)", job.name, job.id)

        return job



    def remove_job(self, job_id: str) -> Literal["removed", "protected", "not_found"]:

        """Remove a job by ID, unless it is a protected system job."""

        store = self._require_store()

        job = next((j for j in store.jobs if j.id == job_id), None)

        if job is None:

            return "not_found"

        if job.payload.kind == "system_event":

            logger.info("Cron: refused to remove protected system job %s", job_id)

            return "protected"



        before = len(store.jobs)

        store.jobs = [j for j in store.jobs if j.id != job_id]

        removed = len(store.jobs) < before



        if removed:

            if self._running:

                self._save_store()

                self._arm_timer()

            else:

                self._append_action("del", {"job_id": job_id})

            logger.info("Cron: removed job %s", job_id)

            return "removed"



        return "not_found"



    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:

        """Enable or disable a job."""

        store = self._require_store()

        for job in store.jobs:

            if job.id == job_id:

                job.enabled = enabled

                job.updated_at_ms = _now_ms()

                self._enforce_agent_binding(job)

                if job.enabled:

                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

                else:

                    job.state.next_run_at_ms = None

                if self._running:

                    self._save_store()

                    self._arm_timer()

                else:

                    self._append_action("update", asdict(job))

                return job

        return None



    def update_job(

        self,

        job_id: str,

        *,

        name: str | None = None,

        schedule: CronSchedule | None = None,

        message: str | None = None,

        deliver: bool | None = None,

        channel: str | None = ...,

        to: str | None = ...,

        delete_after_run: bool | None = None,

    ) -> CronJob | Literal["not_found", "protected"]:

        """Update mutable fields of an existing job. System jobs cannot be updated.



        For ``channel`` and ``to``, pass an explicit value (including ``None``)

        to update; omit (sentinel ``...``) to leave unchanged.

        """

        store = self._require_store()

        job = next((j for j in store.jobs if j.id == job_id), None)

        if job is None:

            return "not_found"

        if job.payload.kind == "system_event":

            return "protected"



        if schedule is not None:

            _validate_schedule_for_add(schedule)

            job.schedule = schedule

        if name is not None:

            job.name = name

        if message is not None:

            job.payload.message = message

        if deliver is not None:

            job.payload.deliver = deliver

        if channel is not ...:

            job.payload.channel = channel

        if to is not ...:

            job.payload.to = to

        if delete_after_run is not None:

            job.delete_after_run = delete_after_run

        _normalize_agent_turn_job(job)

        self._enforce_agent_binding(job)



        job.updated_at_ms = _now_ms()

        if job.enabled:

            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

        else:

            job.state.next_run_at_ms = None



        if self._running:

            self._save_store()

            self._arm_timer()

        else:

            self._append_action("update", asdict(job))



        logger.info("Cron: updated job '%s' (%s)", job.name, job.id)

        return job



    async def run_job(self, job_id: str, force: bool = False) -> bool:

        """Manually run a job without disturbing the service's running state."""

        was_running = self._running

        self._running = True

        try:

            store = self._require_store()

            for job in store.jobs:

                if job.id == job_id:

                    if self._is_unbound_agent_job(job):

                        self._enforce_agent_binding(job)

                        self._save_store()

                        return False

                    if not force and not job.enabled:

                        return False

                    await self._execute_job(job)

                    self._save_store()

                    return True

            return False

        finally:

            self._running = was_running

            if was_running:

                self._arm_timer()



    def get_job(self, job_id: str) -> CronJob | None:

        """Get a job by ID."""

        store = self._require_store()

        return next((j for j in store.jobs if j.id == job_id), None)



    def status(self) -> dict:

        """Get service status."""

        store = self._require_store()

        return {

            "enabled": self._running,

            "jobs": len(store.jobs),

            "next_wake_at_ms": self._get_next_wake_ms(),

            "heartbeat_at_ms": store.heartbeat_at_ms,

            "last_success_at_ms": store.last_success_at_ms,

        }



    # ==================================================================

    # v2: Pause / Resume / Trigger

    # ==================================================================



    def pause_job(self, job_id: str, reason: str = "") -> CronJob | None:

        """Pause a job with an optional reason for operational visibility.



        Unlike ``enable_job(job_id, False)``, this records *when* and

        *why* the job was paused so operators can understand the state.

        """

        store = self._require_store()

        for job in store.jobs:

            if job.id == job_id:

                job.enabled = False

                job.state.paused_at_ms = _now_ms()

                job.state.paused_reason = reason

                job.state.next_run_at_ms = None

                job.updated_at_ms = _now_ms()

                if self._running:

                    self._save_store()

                    self._arm_timer()

                else:

                    self._append_action("update", asdict(job))

                logger.info("Cron: paused job '%s' (%s): %s", job.name, job.id, reason or "(no reason)")

                return job

        return None



    def resume_job(self, job_id: str) -> CronJob | None:

        """Resume a paused job and recompute the next future run."""

        store = self._require_store()

        for job in store.jobs:

            if job.id == job_id:

                job.enabled = True

                job.state.paused_at_ms = None

                job.state.paused_reason = ""

                job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())

                job.updated_at_ms = _now_ms()

                self._enforce_agent_binding(job)

                if self._running:

                    self._save_store()

                    self._arm_timer()

                else:

                    self._append_action("update", asdict(job))

                logger.info("Cron: resumed job '%s' (%s)", job.name, job.id)

                return job

        return None



    def trigger_job(self, job_id: str) -> CronJob | None:

        """Schedule a job to run on the next timer tick (manual fire).



        Sets ``next_run_at_ms`` to now so the next tick picks it up.

        Useful for testing or "run now" buttons.

        """

        store = self._require_store()

        for job in store.jobs:

            if job.id == job_id:

                job.enabled = True

                job.state.paused_at_ms = None

                job.state.paused_reason = ""

                job.state.next_run_at_ms = _now_ms()

                job.updated_at_ms = _now_ms()

                if self._running:

                    self._save_store()

                    self._arm_timer()

                else:

                    self._append_action("update", asdict(job))

                logger.info("Cron: triggered job '%s' (%s) for immediate execution", job.name, job.id)

                return job

        return None



    # ==================================================================

    # v2: Heartbeat liveness

    # ==================================================================



    def _record_heartbeat(self, *, success: bool = False) -> None:

        """Record a ticker liveness signal.



        Called once per timer tick. ``success=True`` additionally bumps

        the last-successful-tick marker so external monitors can

        distinguish "alive but failing every tick" from "alive and

        succeeding".



        Persists immediately so external processes / subsequent

        ``_load_store`` calls observe the fresh heartbeat without

        waiting for the end-of-tick ``_save_store``.

        """

        if not self._store:

            return

        now = _now_ms()

        self._store.heartbeat_at_ms = now

        if success:

            self._store.last_success_at_ms = now

        self._save_store()



    def get_heartbeat_age_seconds(self) -> float | None:

        """Seconds since the ticker last iterated, or None if unknown.



        ``None`` means the heartbeat was never recorded (service never

        started or never completed a tick).

        """

        store = self._require_store()

        if store.heartbeat_at_ms == 0:

            return None

        return max(0.0, (_now_ms() - store.heartbeat_at_ms) / 1000.0)



    def get_success_age_seconds(self) -> float | None:

        """Seconds since the ticker last completed a tick without raising."""

        store = self._require_store()

        if store.last_success_at_ms == 0:

            return None

        return max(0.0, (_now_ms() - store.last_success_at_ms) / 1000.0)



    # ==================================================================

    # v2: Execution output persistence

    # ==================================================================



    def _save_job_output(self, job_id: str, output: str, run_at_ms: int) -> str:

        """Save a job's execution output to a file for audit and deps.



        Returns the path to the saved file, or "" on failure.

        Files are stored under ``runs/<job_id>/<timestamp>.md`` and

        pruned to ``_OUTPUT_RETENTION`` per job.

        """

        try:

            # Sanitize job_id for filesystem safety

            safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in job_id)

            if not safe_id:

                safe_id = "unknown"

            job_dir = self._run_records_dir / safe_id

            job_dir.mkdir(parents=True, exist_ok=True)



            ts = datetime.fromtimestamp(run_at_ms / 1000).strftime("%Y%m%d_%H%M%S")

            output_file = job_dir / f"{ts}.md"



            self._atomic_write(output_file, output)



            # Prune old outputs (reverse-lexical = newest-first)

            files = sorted(

                (f for f in job_dir.glob("*.md") if f.is_file()),

                key=lambda f: f.name,

                reverse=True,

            )

            for stale in files[self._OUTPUT_RETENTION:]:

                with suppress(OSError):

                    stale.unlink()



            return str(output_file)

        except Exception:

            logger.debug("Failed to save cron output for job %s", job_id, exc_info=True)

            return ""



    def get_job_output_dir(self, job_id: str) -> Path | None:

        """Return the output directory for a job, or None if none exists."""

        safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in job_id)

        if not safe_id:

            return None

        d = self._run_records_dir / safe_id

        return d if d.exists() else None



    def get_latest_job_output(self, job_id: str) -> str | None:

        """Return the most recent output content for a job, or None."""

        d = self.get_job_output_dir(job_id)

        if d is None:

            return None

        files = sorted(

            (f for f in d.glob("*.md") if f.is_file()),

            key=lambda f: f.name,

            reverse=True,

        )

        if not files:

            return None

        try:

            return files[0].read_text(encoding="utf-8")

        except OSError:

            return None



    # ==================================================================

    # v2: Dependency context injection

    # ==================================================================



    def _build_dependency_context(self, job: CronJob) -> str:

        """Build context text from the job's ``depends_on`` outputs.



        For each dependency job ID, reads its most recent output file

        and includes it as context. Missing dependencies are silently

        skipped (the job still runs, just without that dep's context).

        """

        if not job.payload.depends_on:

            return ""

        lines: list[str] = []

        for dep_id in job.payload.depends_on:

            output = self.get_latest_job_output(dep_id)

            if output:

                dep_job = self.get_job(dep_id)

                dep_name = dep_job.name if dep_job else dep_id

                lines.append(f"### 依赖任务: {dep_name} ({dep_id})\n{output}")

        return "\n\n".join(lines)



    # ==================================================================

    # v2: Claim-based dispatch

    # ==================================================================



    @staticmethod

    def _machine_id() -> str:

        """Stable-ish identifier for claim attribution."""

        import socket

        try:

            host = socket.gethostname()

        except Exception:

            host = "unknown"

        return f"{host}:{os.getpid()}"



    def _advance_next_run(self, job: CronJob) -> None:

        """Preemptively advance next_run_at for a recurring job before execution.



        Called BEFORE ``_execute_job`` so that if the process crashes

        mid-execution, the job won't re-fire on the next restart.

        Converts the scheduler from at-least-once to at-most-once for

        recurring jobs — missing one run is better than firing dozens

        of times in a crash loop.

        """

        if job.schedule.kind not in ("cron", "every"):

            return

        now = _now_ms()

        new_next = _compute_next_run(job.schedule, now)

        if new_next and new_next != job.state.next_run_at_ms:

            job.state.next_run_at_ms = new_next



    def claim_dispatch(self, job_id: str) -> bool:

        """Atomically claim a finite one-shot job dispatch BEFORE execution.



        Returns True if the caller may proceed to run the job, False if

        the dispatch limit is already reached (in which case the stale

        job is removed). Only applies to jobs with ``repeat_times > 0``.

        """

        store = self._require_store()

        for job in store.jobs:

            if job.id != job_id:

                continue

            if job.repeat_times is None or job.repeat_times <= 0:

                return True  # no limit — always dispatch

            if job.repeat_completed >= job.repeat_times:

                # Already dispatched the max — clean up

                store.jobs = [j for j in store.jobs if j.id != job_id]

                self._save_store()

                logger.info("Cron: job '%s' dispatch limit reached, removed", job.name)

                return False

            # Claim this dispatch

            job.repeat_completed += 1

            self._save_store()

            return True

        return True  # job not in store — proceed without claim
