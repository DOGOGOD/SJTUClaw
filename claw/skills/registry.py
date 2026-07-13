"""Skill registry — v7.

v7 enhancements:

- **Hot reload**: ``rescan()`` detects filesystem changes by tracking
  per-skill SKILL.md modification times, and returns a structured diff
  (added / removed / modified) so callers can invalidate caches
  precisely.
- **Usage statistics**: sidecar ``.usage.json`` (via
  :class:`claw.skills.usage.SkillUsageStore`) tracks use_count /
  view_count / patch_count per skill with atomic cross-process writes.
- **Lifecycle states**: skills transition active → stale → archived
  based on inactivity (``apply_automatic_transitions``).
- **Telemetry hooks**: ``record_use`` / ``record_view`` / ``record_patch``
  bump the sidecar counters from the agent loop and skill tools.

v6 (prior):

- **Full YAML frontmatter** via pyyaml (supports nested structures).
- **Two-tier loading**: built-in skills + workspace skills, with
  workspace taking precedence.
- **Requirements checking**: ``requires: {bins: [...], env: [...]}``
  — skills with unmet deps are marked unavailable.
- **Always-on skills**: ``always: true`` auto-injects into LLM context.
- **Progressive loading**: context only gets name+description+path;
  full instructions loaded on demand via ``read_file``.
- **Disabled skills**: configurable via ``disabled_skills`` set.
- **Structured claw metadata**: ``metadata: {claw: {always, requires}}``
  for runtime behavior configuration.
- **Skill availability**: filter by requirements, show missing deps.
- **Built-in skills directory**: ships with framework defaults.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from claw.config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SKILLS_DIR = PROJECT_ROOT / "skills"

# Frontmatter regex (for stripping YAML)
_STRIP_FRONTMATTER_RE = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)
_VALID_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


from claw.utils import now_iso as _now_iso


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SkillRegistryError(RuntimeError):
    """Raised when a skill cannot be found or parsed."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SkillInfo:
    """Full information about one skill."""

    name: str
    description: str
    instructions: str
    directory: Path
    source: str = "workspace"
    assets: list[Path] = field(default_factory=list)
    references: list[Path] = field(default_factory=list)

    # -- claw metadata --
    always: bool = False           # auto-inject into context
    disabled: bool = False         # explicitly disabled
    requires_bins: list[str] = field(default_factory=list)   # required CLI tools
    requires_env: list[str] = field(default_factory=list)    # required env vars
    available: bool = True         # whether requirements are met
    missing_deps: str = ""         # human-readable missing deps

    @property
    def index_entry(self) -> dict[str, str]:
        """Lightweight index entry for context embedding."""
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.directory / "SKILL.md"),
            "available": str(self.available),
            "source": self.source,
        }

    @property
    def is_available(self) -> bool:
        return self.available and not self.disabled


@dataclass
class SkillUsageRecord:
    """One skill invocation persisted in a conversation session."""

    skill_name: str
    session_id: str
    user_task: str
    source: str
    auto_reason: str = ""
    used_at: str = field(default_factory=_now_iso)
    output_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skillName": self.skill_name,
            "sessionId": self.session_id,
            "userTask": self.user_task,
            "source": self.source,
            "autoReason": self.auto_reason,
            "usedAt": self.used_at,
            "outputPath": self.output_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillUsageRecord":
        return cls(
            skill_name=str(data.get("skillName", "")),
            session_id=str(data.get("sessionId", "")),
            user_task=str(data.get("userTask", "")),
            source=str(data.get("source", "explicit")),
            auto_reason=str(data.get("autoReason", "")),
            used_at=str(data.get("usedAt", "")),
            output_path=str(data.get("outputPath", "")),
        )


# ---------------------------------------------------------------------------
# YAML frontmatter helpers
# ---------------------------------------------------------------------------


def parse_frontmatter(raw: str) -> dict[str, Any]:
    """Parse YAML frontmatter from a SKILL.md string using pyyaml.

    Returns a dict with keys: name, description, instructions, and any
    claw metadata (always, requires, etc.).
    """
    if not raw.startswith("---"):
        raise SkillRegistryError("SKILL.md 格式错误：缺少 frontmatter (--- 分隔符)")

    match = _STRIP_FRONTMATTER_RE.match(raw)
    if not match:
        raise SkillRegistryError("SKILL.md 格式错误：frontmatter 格式无效")

    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillRegistryError(f"YAML 解析失败: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SkillRegistryError("frontmatter 必须是 YAML mapping")

    meta: dict[str, Any] = {str(k): v for k, v in parsed.items()}
    meta["instructions"] = raw[match.end():].strip()

    return meta


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from content."""
    if not content.startswith("---"):
        return content
    match = _STRIP_FRONTMATTER_RE.match(content)
    if match:
        return content[match.end():].strip()
    return content


def _parse_claw_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """Extract claw metadata from a frontmatter field."""
    raw = meta.get("metadata", {})
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("claw", data.get("openclaw", {}))


def _check_requirements(requires_bins: list[str], requires_env: list[str]) -> tuple[bool, str]:
    """Check if skill requirements are met."""
    missing: list[str] = []
    for cmd in requires_bins:
        if not shutil.which(str(cmd)):
            missing.append(f"CLI: {cmd}")
    for var in requires_env:
        if not os.environ.get(str(var)):
            missing.append(f"ENV: {var}")
    return (len(missing) == 0, ", ".join(missing))


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Scan, index and load skills — v7.

    All skills are stored under the single ``skills/`` directory at the
    project root.  Each skill is a subdirectory containing a ``SKILL.md``
    with YAML frontmatter.

    Progressive loading:
    - ``list_index()`` → name + description only (lightweight, injected into context).
    - ``load_skill()`` / ``read_file`` → full instructions (on demand).
    - ``get_always_skills()`` → skills with ``always: true``, auto-injected.

    v7 additions:
    - ``rescan()`` — hot-reloads skills from disk and returns a diff
      (added/removed/modified) so callers can invalidate caches.
    - ``record_use`` / ``record_view`` / ``record_patch`` — bump the
      sidecar usage counters (best-effort, never blocks).
    - ``get_usage_stats`` / ``get_usage_report`` — read telemetry.
    - ``apply_lifecycle_transitions`` — auto-transition stale/archived.

    Usage::

        reg = SkillRegistry(disabled_skills={"deprecated_tool"})
        index = reg.list_index()
        always = reg.get_always_skills()
        full  = reg.format_full_content("my-skill")
        diff  = reg.rescan()  # hot-reload after editing SKILL.md
    """

    def __init__(
        self,
        skills_dir: Path | None = None,
        disabled_skills: set[str] | None = None,
    ):
        self._skills_dir = skills_dir or SKILLS_DIR
        self._disabled = disabled_skills or set()
        self._skills: dict[str, SkillInfo] = {}
        self._load_errors: list[str] = []
        # Bumped on every rescan so consumers can invalidate caches.
        self._version: int = 0
        # v7: per-skill SKILL.md mtime tracking for hot-reload diffing.
        self._skill_mtimes: dict[str, float] = {}
        # v7: sidecar usage statistics store.
        from claw.skills.usage import SkillUsageStore
        self._usage_store = SkillUsageStore(self._skills_dir)
        self._scan()

    @property
    def version(self) -> int:
        """Monotonically increasing version counter.

        Incremented whenever ``_scan()`` reloads skills from disk.
        Consumers can use this to detect whether their cached
        skill-block snapshot is stale.
        """
        return self._version

    @property
    def load_errors(self) -> list[str]:
        """Diagnostics from the latest scan, without aborting healthy skills."""
        return list(self._load_errors)

    # ------------------------------------------------------------------
    # Public API — listing
    # ------------------------------------------------------------------

    def list_index(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """Return lightweight skill index for LLM context embedding."""
        skills = self.list_skills(filter_unavailable=filter_unavailable)
        return [s.index_entry for s in skills]

    def list_skills(self, filter_unavailable: bool = True) -> list[SkillInfo]:
        """Return all loaded skills, optionally filtering unavailable ones."""
        result = list(self._skills.values())
        if filter_unavailable:
            result = [s for s in result if s.is_available]
        return sorted(result, key=lambda s: s.name)

    def get_skill(self, name: str) -> SkillInfo | None:
        """Return skill metadata by name, or None."""
        return self._skills.get(name)

    def get_always_skills(self) -> list[str]:
        """Return names of skills with ``always: true`` that meet requirements."""
        return [
            s.name for s in self._skills.values()
            if s.always and s.is_available
        ]

    def get_skill_availability(self, name: str) -> tuple[bool, str]:
        """Return (available, why_not)."""
        skill = self._skills.get(name)
        if skill is None:
            return False, f"未找到 skill: {name}"
        if skill.disabled:
            return False, "已禁用"
        if not skill.available:
            return False, skill.missing_deps or "不满足依赖要求"
        return True, ""

    def get_skill_requirements(self, name: str) -> dict[str, Any]:
        """Return requirement details for a skill."""
        skill = self._skills.get(name)
        if skill is None:
            return {"bins": [], "env": [], "missingBins": [], "missingEnv": []}
        missing_bins = [b for b in skill.requires_bins if not shutil.which(str(b))]
        missing_env = [e for e in skill.requires_env if not os.environ.get(str(e))]
        return {
            "bins": skill.requires_bins,
            "env": skill.requires_env,
            "missingBins": missing_bins,
            "missingEnv": missing_env,
        }

    # ------------------------------------------------------------------
    # Public API — loading
    # ------------------------------------------------------------------

    def load_skill(self, name: str) -> SkillInfo:
        """Return full SkillInfo for *name* (includes instructions).

        Raises SkillRegistryError if not found.
        """
        skill = self._skills.get(name)
        if skill is None:
            available = sorted(self._skills.keys())
            raise SkillRegistryError(
                f"未找到 skill: \"{name}\"。可用 skill: {available}"
            )
        return skill

    def format_full_content(self, name: str) -> str:
        """Return the full skill content as a string for LLM context injection."""
        skill = self.load_skill(name)
        parts = [
            f"## Skill: {skill.name}",
            f"描述: {skill.description}",
            "",
            "### 使用说明",
            skill.instructions,
        ]
        if skill.assets:
            parts.append("\n### 附带资源 (assets)\n" + "\n".join(
                f"- {a.relative_to(skill.directory)}" for a in skill.assets
            ))
        if skill.references:
            parts.append("\n### 参考文件 (references)\n" + "\n".join(
                f"- {r.relative_to(skill.directory)}" for r in skill.references
            ))
        return "\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """Build a Markdown summary of all skills for context injection.

        Includes availability status and missing dependencies.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        lines: list[str] = []
        for skill in all_skills:
            if exclude and skill.name in exclude:
                continue
            path = str(skill.directory / "SKILL.md")
            if skill.is_available:
                lines.append(f"- **{skill.name}** — {skill.description}  `{path}`")
            else:
                suffix = f" (unavailable: {skill.missing_deps})" if skill.missing_deps else " (unavailable)"
                lines.append(f"- **{skill.name}** — {skill.description}{suffix}  `{path}`")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Scan ``skills/`` directory and load all skills.

        v7: also records per-skill SKILL.md modification times so
        ``rescan()`` can detect changes without re-parsing every file.
        """
        self._skills.clear()
        self._skill_mtimes.clear()
        self._load_errors.clear()
        self._version += 1

        if self._skills_dir.exists():
            for entry in self._iter_skill_directories():
                skill = self._try_parse_skill(entry)
                if skill:
                    if skill.name in self._skills:
                        message = (
                            f"Skill 名称冲突: {skill.name!r} 同时出现在 "
                            f"{self._skills[skill.name].directory} 和 {entry}"
                        )
                        self._load_errors.append(message)
                        print(f"[skill] 警告: {message}")
                        continue
                    self._skills[skill.name] = skill
                    self._record_skill_mtime(skill)

        # Apply disabled list
        for name in self._disabled:
            if name in self._skills:
                self._skills[name].disabled = True

    def _iter_skill_directories(self) -> list[Path]:
        """Return flat skills plus one optional category level."""
        if not self._skills_dir.exists():
            return []
        result: list[Path] = []
        for entry in sorted(self._skills_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if (entry / "SKILL.md").is_file():
                result.append(entry)
                continue
            for child in sorted(entry.iterdir()):
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and (child / "SKILL.md").is_file()
                ):
                    result.append(child)
        return result

    def _record_skill_mtime(self, skill: SkillInfo) -> None:
        """Record the current mtime of a skill's SKILL.md for diffing."""
        try:
            skill_md = skill.directory / "SKILL.md"
            if skill_md.exists():
                self._skill_mtimes[skill.name] = float(skill_md.stat().st_mtime_ns)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # v7: Hot reload
    # ------------------------------------------------------------------

    def rescan(self, *, force: bool = False) -> dict[str, list[str]]:
        """Hot-reload skills from disk and return a structured diff.

        Returns a dict with keys ``added``, ``removed``, ``modified``,
        each a list of skill names. Only re-parses files whose mtime
        changed; untouched skills keep their existing SkillInfo.

        Callers can use the return value to invalidate caches precisely
        rather than rebuilding their entire skill block on every poll.
        """
        before_names = set(self._skills.keys())
        if force:
            self._scan()
            after_names = set(self._skills.keys())
            return {
                "added": sorted(after_names - before_names),
                "removed": sorted(before_names - after_names),
                "modified": sorted(before_names & after_names),
            }
        before_mtimes = dict(self._skill_mtimes)

        # Re-scan the filesystem to discover current mtimes.
        current_mtimes: dict[str, float] = {}
        current_dirs: dict[str, Path] = {}

        base = self._skills_dir
        if base and base.exists():
            for entry in self._iter_skill_directories():
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    continue
                # Parse just the name to key the mtime map (cheap peek).
                try:
                    raw = skill_md.read_text(encoding="utf-8")
                    meta = parse_frontmatter(raw)
                    name = str(meta.get("name", "")).strip()
                    if name:
                        current_mtimes[name] = float(skill_md.stat().st_mtime_ns)
                        current_dirs[name] = entry
                except (SkillRegistryError, OSError):
                    continue

        current_names = set(current_mtimes.keys())

        added = sorted(current_names - before_names)
        removed = sorted(before_names - current_names)
        modified: list[str] = []
        for name in current_names & before_names:
            old_mt = before_mtimes.get(name, 0.0)
            new_mt = current_mtimes.get(name, 0.0)
            if new_mt != old_mt:
                modified.append(name)
        modified.sort()

        # If anything changed, re-parse only the affected skills.
        changed = added + modified
        if changed or removed:
            for name in removed:
                self._skills.pop(name, None)
                self._skill_mtimes.pop(name, None)
            for name in changed:
                directory = current_dirs.get(name)
                if directory is None:
                    continue
                skill = self._try_parse_skill(directory)
                if skill:
                    self._skills[name] = skill
                    self._record_skill_mtime(skill)
            self._version += 1
            # Re-apply disabled list.
            for name in self._disabled:
                if name in self._skills:
                    self._skills[name].disabled = True

        return {"added": added, "removed": removed, "modified": modified}

    # ------------------------------------------------------------------
    # v7: Usage telemetry
    # ------------------------------------------------------------------

    def record_use(self, skill_name: str) -> None:
        """Bump the use counter for *skill_name* (best-effort)."""
        self._usage_store.bump_use(skill_name)

    def record_view(self, skill_name: str) -> None:
        """Bump the view counter for *skill_name* (best-effort)."""
        self._usage_store.bump_view(skill_name)

    def record_patch(self, skill_name: str) -> None:
        """Bump the patch counter for *skill_name* (best-effort)."""
        self._usage_store.bump_patch(skill_name)

    def get_usage_stats(self, skill_name: str) -> dict[str, Any]:
        """Return the usage record for a single skill (backfilled with defaults)."""
        return self._usage_store.get_record(skill_name)

    def get_usage_report(self) -> list[dict[str, Any]]:
        """Return usage telemetry for every known skill in the registry."""
        names = [s.name for s in self._skills.values()]
        return self._usage_store.usage_report(names)

    def apply_lifecycle_transitions(
        self,
        *,
        stale_after_days: int = 30,
        archive_after_days: int = 90,
    ) -> dict[str, str]:
        """Auto-transition skills to stale/archived based on inactivity.

        Returns a mapping of ``{skill_name: "stale"|"archived"}`` for
        skills whose state changed. Pinned skills are skipped.
        """
        names = [s.name for s in self._skills.values()]
        return self._usage_store.apply_automatic_transitions(
            names,
            stale_after_days=stale_after_days,
            archive_after_days=archive_after_days,
        )

    @property
    def usage_store(self):
        """Direct access to the underlying SkillUsageStore (for advanced use)."""
        return self._usage_store

    def _try_parse_skill(self, directory: Path) -> SkillInfo | None:
        """Try to parse a skill directory. Returns None on failure."""
        skill_md = directory / "SKILL.md"
        if not skill_md.exists():
            return None

        try:
            raw = skill_md.read_text(encoding="utf-8")
            return self._parse_skill(directory, raw)
        except (SkillRegistryError, OSError, UnicodeError) as exc:
            message = f"无法加载 Skill {directory.name!r}: {exc}"
            self._load_errors.append(message)
            print(f"[skill] 警告: {message}")
            return None

    def _parse_skill(self, directory: Path, raw: str) -> SkillInfo:
        """Parse a SKILL.md file into a SkillInfo."""
        meta = parse_frontmatter(raw)

        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        instructions = str(meta.get("instructions", ""))

        if not name:
            raise SkillRegistryError("缺少 name 字段")
        if not _VALID_SKILL_NAME_RE.fullmatch(name):
            raise SkillRegistryError(f"name 字段格式无效: {name!r}")
        if name != directory.name:
            raise SkillRegistryError(
                f"name {name!r} 必须与目录名 {directory.name!r} 一致"
            )
        if not description:
            raise SkillRegistryError("缺少 description 字段")
        if not instructions:
            raise SkillRegistryError("instructions 为空")

        # Extract claw metadata
        nm = _parse_claw_metadata(meta)
        raw_always = meta.get("always", nm.get("always", False))
        always = raw_always is True or (
            isinstance(raw_always, str) and raw_always.strip().lower() in {"1", "true", "yes", "on"}
        )
        requires = nm.get("requires", meta.get("requires", {}))
        if requires is None:
            requires = {}
        if not isinstance(requires, dict):
            raise SkillRegistryError("requires 必须是 mapping")

        def _requirement_list(key: str) -> list[str]:
            value = requires.get(key, [])
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                raise SkillRegistryError(f"requires.{key} 必须是字符串列表")
            return [str(item).strip() for item in value if str(item).strip()]

        requires_bins = _requirement_list("bins")
        requires_env = _requirement_list("env")

        available, missing_deps = _check_requirements(requires_bins, requires_env)

        # Collect assets and references
        assets: list[Path] = []
        refs: list[Path] = []
        for sub, target in [("assets", assets), ("references", refs)]:
            subdir = directory / sub
            if subdir.is_dir():
                target.extend(sorted(p for p in subdir.iterdir() if p.is_file()))

        return SkillInfo(
            name=name,
            description=description,
            instructions=instructions,
            directory=directory,
            source="workspace",
            assets=assets,
            references=refs,
            always=always,
            requires_bins=requires_bins,
            requires_env=requires_env,
            available=available,
            missing_deps=missing_deps,
        )
