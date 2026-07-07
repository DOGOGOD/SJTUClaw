"""Skill registry: scan, index, load skills from the ``skills/`` directory.

A ``SKILL.md`` MUST begin with YAML-style frontmatter delimited by
``---`` lines. The frontmatter must contain:

    name: <skill-name>
    description: >
      one-line description of what this skill does and when to use it.

Everything after the second ``---`` is the *instructions* body.

The registry reads everything at startup; scanning is intentionally
done once to avoid filesystem I/O inside the agent loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claw.config import PROJECT_ROOT

SKILLS_DIR = PROJECT_ROOT / "skills"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse YAML-style frontmatter from *text*.

    Returns a dict with keys ``name``, ``description``, and the raw
    ``instructions`` text.  Values are stripped of surrounding whitespace.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillRegistryError("SKILL.md 格式错误：缺少 frontmatter (--- 分隔符)")

    frontmatter_raw = m.group(1)
    instructions = m.group(2).strip()

    meta: dict[str, str] = {}
    key: str | None = None
    value_lines: list[str] = []

    for line in frontmatter_raw.splitlines():
        kv_match = re.match(r"^(\w[\w-]*)\s*:\s*(.*)", line)
        if kv_match:
            # Flush previous key
            if key is not None:
                meta[key] = " ".join(value_lines).strip()
            key = kv_match.group(1)
            value_lines = [kv_match.group(2).strip()]
        else:
            stripped = line.strip()
            if stripped and key is not None:
                # Continuation line (multi-line YAML value)
                value_lines.append(stripped)

    if key is not None:
        meta[key] = " ".join(value_lines).strip()

    meta["instructions"] = instructions
    return meta


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SkillInfo:
    """Full information about one skill (loaded on demand)."""

    name: str
    description: str
    instructions: str
    directory: Path  # the skill's directory under skills/
    assets: list[Path] = field(default_factory=list)
    references: list[Path] = field(default_factory=list)

    @property
    def index_entry(self) -> dict[str, str]:
        """Return the lightweight index entry for context embedding."""
        return {"name": self.name, "description": self.description}


@dataclass
class SkillUsageRecord:
    """Record of one skill usage in a session."""

    skill_name: str
    session_id: str
    user_task: str
    source: str  # "explicit" or "auto"
    auto_reason: str = ""  # only set when source == "auto"
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
# Errors
# ---------------------------------------------------------------------------


class SkillRegistryError(RuntimeError):
    """Raised when a skill cannot be found or parsed."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Scan, index and load skills from ``skills/``.

    Usage::

        reg = SkillRegistry()
        index = reg.list_index()                  # lightweight list
        full  = reg.load_skill("course-report")   # full instructions
    """

    def __init__(self, skills_dir: Path | None = None):
        self._skills_dir = skills_dir or SKILLS_DIR
        self._skills: dict[str, SkillInfo] = {}
        self._scan()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_index(self) -> list[dict[str, str]]:
        """Return lightweight skill index for embedding in LLM context.

        Only ``name`` and ``description`` — NOT the full instructions.
        """
        return [s.index_entry for s in self._skills.values()]

    def list_skills(self) -> list[SkillInfo]:
        """Return all loaded skills (metadata only, no instructions loaded yet)."""
        return list(self._skills.values())

    def get_skill(self, name: str) -> SkillInfo | None:
        """Return the skill metadata by name, or None."""
        return self._skills.get(name)

    def load_skill(self, name: str) -> SkillInfo:
        """Return the full ``SkillInfo`` for *name* (including instructions).

        Raises ``SkillRegistryError`` if not found.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise SkillRegistryError(
                f"未找到 skill: \"{name}\"。"
                f"可用 skill: {sorted(self._skills.keys())}"
            )
        return skill

    def format_full_content(self, name: str) -> str:
        """Return the full skill content as a string suitable for injection
        into the LLM context.

        Includes name, description, instructions, and references to
        assets/references files (paths only, not their full content).
        """
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

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Scan ``skills/`` for subdirectories containing ``SKILL.md``."""
        if not self._skills_dir.exists():
            return

        for entry in sorted(self._skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                skill = self._parse_skill(entry, skill_md)
                self._skills[skill.name] = skill
            except SkillRegistryError as exc:
                print(f"[skill] 警告: 无法加载 skill \"{entry.name}\": {exc}")

    def _parse_skill(self, directory: Path, skill_md: Path) -> SkillInfo:
        """Parse a single SKILL.md file into a ``SkillInfo``."""
        raw = skill_md.read_text(encoding="utf-8")
        meta = _parse_frontmatter(raw)

        name = meta.get("name", "").strip()
        description = meta.get("description", "").strip()
        instructions = meta.get("instructions", "")

        if not name:
            raise SkillRegistryError("缺少 name 字段")
        if not description:
            raise SkillRegistryError("缺少 description 字段")
        if not instructions:
            raise SkillRegistryError("instructions 为空")

        # Collect assets and references
        assets: list[Path] = []
        refs: list[Path] = []

        assets_dir = directory / "assets"
        if assets_dir.is_dir():
            assets = sorted(
                p for p in assets_dir.iterdir() if p.is_file()
            )

        refs_dir = directory / "references"
        if refs_dir.is_dir():
            refs = sorted(
                p for p in refs_dir.iterdir() if p.is_file()
            )

        return SkillInfo(
            name=name,
            description=description,
            instructions=instructions,
            directory=directory,
            assets=assets,
            references=refs,
        )
