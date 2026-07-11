"""Skill registry — v6.

Enhancements over v5:

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
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills_builtin"

# Frontmatter regex (for stripping YAML)
_STRIP_FRONTMATTER_RE = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    source: str = "workspace"  # "builtin" | "workspace"
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
    """Record of one skill usage in a session."""

    skill_name: str
    session_id: str
    user_task: str
    source: str  # "explicit" | "auto"
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
    def from_dict(cls, data: dict) -> "SkillUsageRecord":
        return cls(
            skill_name=data.get("skillName", ""),
            session_id=data.get("sessionId", ""),
            user_task=data.get("userTask", ""),
            source=data.get("source", "explicit"),
            auto_reason=data.get("autoReason", ""),
            used_at=data.get("usedAt", ""),
            output_path=data.get("outputPath", ""),
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
    """Scan, index and load skills — v6.

    Two-tier loading:
    1. **Built-in skills** (``skills_builtin/``) — shipped with claw.
    2. **Workspace skills** (``skills/``) — user-created, override built-in.

    Progressive loading:
    - ``list_index()`` → name + description only (lightweight, injected into context).
    - ``load_skill()`` / ``read_file`` → full instructions (on demand).
    - ``get_always_skills()`` → skills with ``always: true``, auto-injected.

    Usage::

        reg = SkillRegistry(disabled_skills={"deprecated_tool"})
        index = reg.list_index()
        always = reg.get_always_skills()
        full  = reg.format_full_content("my-skill")
    """

    def __init__(
        self,
        skills_dir: Path | None = None,
        builtin_dir: Path | None = None,
        disabled_skills: set[str] | None = None,
    ):
        self._skills_dir = skills_dir or SKILLS_DIR
        self._builtin_dir = builtin_dir or BUILTIN_SKILLS_DIR
        self._disabled = disabled_skills or set()
        self._skills: dict[str, SkillInfo] = {}
        self._scan()

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
        """Two-tier scan: built-in first, then workspace (override)."""
        self._skills.clear()

        # Tier 1: built-in skills
        if self._builtin_dir and self._builtin_dir.exists():
            for entry in sorted(self._builtin_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill = self._try_parse_skill(entry, "builtin")
                if skill:
                    self._skills[skill.name] = skill

        # Tier 2: workspace skills (override built-in)
        if self._skills_dir.exists():
            for entry in sorted(self._skills_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill = self._try_parse_skill(entry, "workspace")
                if skill:
                    self._skills[skill.name] = skill  # override

        # Apply disabled list
        for name in self._disabled:
            if name in self._skills:
                self._skills[name].disabled = True

    def _try_parse_skill(self, directory: Path, source: str) -> SkillInfo | None:
        """Try to parse a skill directory. Returns None on failure."""
        skill_md = directory / "SKILL.md"
        if not skill_md.exists():
            return None

        try:
            raw = skill_md.read_text(encoding="utf-8")
            return self._parse_skill(directory, raw, source)
        except SkillRegistryError as exc:
            print(f"[skill] 警告: 无法加载 skill \"{directory.name}\" ({source}): {exc}")
            return None

    def _parse_skill(self, directory: Path, raw: str, source: str) -> SkillInfo:
        """Parse a SKILL.md file into a SkillInfo."""
        meta = parse_frontmatter(raw)

        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        instructions = str(meta.get("instructions", ""))

        if not name:
            raise SkillRegistryError("缺少 name 字段")
        if not description:
            raise SkillRegistryError("缺少 description 字段")
        if not instructions:
            raise SkillRegistryError("instructions 为空")

        # Extract claw metadata
        nm = _parse_claw_metadata(meta)
        always = bool(meta.get("always", nm.get("always", False)))
        requires = nm.get("requires", meta.get("requires", {}))
        requires_bins = [str(b) for b in requires.get("bins", [])]
        requires_env = [str(e) for e in requires.get("env", [])]

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
            source=source,
            assets=assets,
            references=refs,
            always=always,
            requires_bins=requires_bins,
            requires_env=requires_env,
            available=available,
            missing_deps=missing_deps,
        )
