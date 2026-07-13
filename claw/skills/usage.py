"""Skill usage telemetry — sidecar JSON statistics.

Tracks per-skill usage metadata in a sidecar ``.usage.json`` file located
in the skills directory. Counters are bumped by the skill tools and the
agent loop; the registry reads the derived activity timestamp to decide
lifecycle transitions.

Design notes (follows ``tools/skill_usage.py``):
  - Sidecar, not frontmatter. Keeps operational telemetry out of
    user-authored SKILL.md content and avoids conflict pressure.
  - Atomic writes via tempfile + os.replace.
  - All counter bumps are best-effort: failures log at DEBUG and return
    silently. A broken sidecar never breaks the underlying tool call.
  - Cross-process safety via filelock (same library used by CronService).

Lifecycle states:
    active    -> default
    stale     -> unused > stale_after_days (config, default 30)
    archived  -> unused > archive_after_days (config, default 90)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from filelock import FileLock

logger = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}

# Default inactivity thresholds (days).
DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90


from claw.utils import now_iso as _now_iso


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp defensively for activity comparisons."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _empty_record() -> Dict[str, Any]:
    return {
        "use_count": 0,
        "view_count": 0,
        "patch_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "last_patched_at": None,
        "created_at": _now_iso(),
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
    }


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """Return the newest actual activity timestamp for a usage record.

    "Activity" means a skill was used, viewed, or patched. Creation time
    is intentionally excluded so callers can still distinguish
    never-active skills.
    """
    latest_dt: Optional[datetime] = None
    latest_raw: Optional[str] = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        dt = _parse_iso_timestamp(raw)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_raw = str(raw)
    return latest_raw


def activity_count(record: Dict[str, Any]) -> int:
    """Return the total observed activity count across use/view/patch events."""
    total = 0
    for key in ("use_count", "view_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


class SkillUsageStore:
    """Sidecar JSON store for skill usage telemetry.

    The store lives at ``<skills_dir>/.usage.json`` and is serialized via
    atomic writes. A companion ``.usage.json.lock`` file guards
    read-modify-write cycles across processes (via ``filelock``).
    """

    def __init__(self, skills_dir: Path | str):
        self._skills_dir = Path(skills_dir)
        self._usage_file = self._skills_dir / ".usage.json"
        self._lock = FileLock(str(self._usage_file) + ".lock")

    @property
    def usage_file(self) -> Path:
        return self._usage_file

    # ------------------------------------------------------------------
    # Sidecar I/O
    # ------------------------------------------------------------------

    def load_usage(self) -> Dict[str, Dict[str, Any]]:
        """Read the entire .usage.json map. Returns empty dict on missing/corrupt."""
        if not self._usage_file.exists():
            return {}
        try:
            data = json.loads(self._usage_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("Failed to read %s: %s", self._usage_file, e)
            return {}
        if not isinstance(data, dict):
            return {}
        clean: Dict[str, Dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                clean[str(k)] = v
        return clean

    def save_usage(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Write the usage map atomically. Best-effort — errors are logged."""
        try:
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._skills_dir), prefix=".usage_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._usage_file)
            except BaseException:
                suppress_file_error(tmp_path)
                raise
        except Exception as e:
            logger.debug("Failed to write %s: %s", self._usage_file, e, exc_info=True)

    def get_record(self, skill_name: str) -> Dict[str, Any]:
        """Return the record for *skill_name*, creating a fresh one if missing."""
        data = self.load_usage()
        rec = data.get(skill_name)
        if not isinstance(rec, dict):
            return _empty_record()
        base = _empty_record()
        for k, v in base.items():
            rec.setdefault(k, v)
        return rec

    # ------------------------------------------------------------------
    # Counter bumps (telemetry for ALL skills — observability only)
    # ------------------------------------------------------------------

    def _mutate(self, skill_name: str, mutator) -> None:
        """Load, apply *mutator(record)* in place, save. Best-effort."""
        if not skill_name:
            return
        try:
            with self._lock:
                data = self.load_usage()
                rec = data.get(skill_name)
                if not isinstance(rec, dict):
                    rec = _empty_record()
                mutator(rec)
                data[skill_name] = rec
                self.save_usage(data)
        except Exception as e:
            logger.debug("skill_usage._mutate(%s) failed: %s", skill_name, e, exc_info=True)

    def bump_use(self, skill_name: str) -> None:
        """Bump use_count and last_used_at. Called when a skill is actively used."""
        def _apply(rec: Dict[str, Any]) -> None:
            rec["use_count"] = int(rec.get("use_count") or 0) + 1
            rec["last_used_at"] = _now_iso()
        self._mutate(skill_name, _apply)

    def bump_view(self, skill_name: str) -> None:
        """Bump view_count and last_viewed_at. Called when skill content is read."""
        def _apply(rec: Dict[str, Any]) -> None:
            rec["view_count"] = int(rec.get("view_count") or 0) + 1
            rec["last_viewed_at"] = _now_iso()
        self._mutate(skill_name, _apply)

    def bump_patch(self, skill_name: str) -> None:
        """Bump patch_count and last_patched_at. Called when a skill is edited."""
        def _apply(rec: Dict[str, Any]) -> None:
            rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
            rec["last_patched_at"] = _now_iso()
        self._mutate(skill_name, _apply)

    # ------------------------------------------------------------------
    # Lifecycle state management
    # ------------------------------------------------------------------

    def set_state(self, skill_name: str, state: str) -> None:
        """Set lifecycle state. No-op if *state* is invalid."""
        if state not in _VALID_STATES:
            logger.debug("set_state: invalid state %r for %s", state, skill_name)
            return

        def _apply(rec: Dict[str, Any]) -> None:
            rec["state"] = state
            if state == STATE_ARCHIVED:
                rec["archived_at"] = _now_iso()
            elif state == STATE_ACTIVE:
                rec["archived_at"] = None
        self._mutate(skill_name, _apply)

    def set_pinned(self, skill_name: str, pinned: bool) -> None:
        def _apply(rec: Dict[str, Any]) -> None:
            rec["pinned"] = bool(pinned)
        self._mutate(skill_name, _apply)

    def forget(self, skill_name: str) -> None:
        """Drop a skill's usage entry entirely. Called when the skill is deleted."""
        if not skill_name:
            return
        try:
            with self._lock:
                data = self.load_usage()
                if skill_name in data:
                    del data[skill_name]
                    self.save_usage(data)
        except Exception as e:
            logger.debug("skill_usage.forget(%s) failed: %s", skill_name, e, exc_info=True)

    # ------------------------------------------------------------------
    # Automatic lifecycle transitions (curator-style)
    # ------------------------------------------------------------------

    def apply_automatic_transitions(
        self,
        known_skills: List[str],
        *,
        stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
        archive_after_days: int = DEFAULT_ARCHIVE_AFTER_DAYS,
    ) -> Dict[str, str]:
        """Walk all known skills and transition stale/archived states.

        Returns a mapping of ``{skill_name: "stale"|"archived"|""}`` for
        skills whose state changed (empty string = no change).
        """
        if not known_skills:
            return {}
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=stale_after_days)
        archive_cutoff = now - timedelta(days=archive_after_days)

        data = self.load_usage()
        changes: Dict[str, str] = {}

        for name in known_skills:
            rec = data.get(name)
            if not isinstance(rec, dict):
                # No record yet — seed one so the clock starts now.
                data[name] = _empty_record()
                continue
            if rec.get("pinned"):
                # Pinned skills never auto-transition.
                continue

            base = _empty_record()
            for k, v in base.items():
                rec.setdefault(k, v)

            current_state = rec.get("state", STATE_ACTIVE)
            if current_state == STATE_ARCHIVED:
                continue

            last_activity = latest_activity_at(rec)
            if last_activity is None:
                # Never active — use created_at as the anchor.
                anchor = _parse_iso_timestamp(rec.get("created_at"))
            else:
                anchor = _parse_iso_timestamp(last_activity)

            if anchor is None:
                continue

            if anchor < archive_cutoff and current_state != STATE_ARCHIVED:
                rec["state"] = STATE_ARCHIVED
                rec["archived_at"] = _now_iso()
                changes[name] = STATE_ARCHIVED
            elif anchor < stale_cutoff and current_state == STATE_ACTIVE:
                rec["state"] = STATE_STALE
                changes[name] = STATE_STALE

        if changes:
            self.save_usage(data)
        return changes

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def usage_report(self, known_skills: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Return usage telemetry for every known skill.

        Each row carries ``last_activity_at`` and ``activity_count`` for
        convenience. Skills with no persisted record are backfilled with
        defaults and flagged via ``_persisted: False``.
        """
        data = self.load_usage()
        if known_skills is None:
            # Return only skills with persisted records.
            names = sorted(data.keys())
        else:
            names = sorted(known_skills)

        rows: List[Dict[str, Any]] = []
        for name in names:
            raw = data.get(name)
            persisted = isinstance(raw, dict)
            rec: Dict[str, Any] = raw if isinstance(raw, dict) else _empty_record()
            base = _empty_record()
            for k, v in base.items():
                rec.setdefault(k, v)
            row = {"name": name, **rec, "_persisted": persisted}
            row["last_activity_at"] = latest_activity_at(row)
            row["activity_count"] = activity_count(row)
            rows.append(row)
        return rows


def suppress_file_error(path: str | Path) -> None:
    """Suppress OSError when unlinking a temp file (best-effort cleanup)."""
    try:
        os.unlink(str(path))
    except OSError:
        pass
