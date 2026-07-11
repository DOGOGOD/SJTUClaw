"""Loading of stable, file-backed prompt content — v5.

Enhanced prompt system supporting:

- **Identity template**: rendered with runtime variables (workspace, OS, channel).
- **Platform policy**: OS-specific guidance (Windows vs POSIX).
- **Tool contract**: tool usage rules injected into every system prompt.
- **Soul & User**: distinct personality (SOUL.md) and user profile (USER.md).
- **Bootstrap files**: AGENTS.md, SOUL.md, USER.md from workspace root.
- **Template detection**: only injects memory/soul content when it differs
  from the built-in default template (avoids polluting context with defaults).
- **Hot-reload**: update system prompt / soul at runtime without restart.
"""

from __future__ import annotations

import platform as _platform
from pathlib import Path

from claw.config import PROJECT_ROOT
from claw.prompts.templates import render, render_file

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.md"
SOUL_PATH = PROMPTS_DIR / "soul.md"
IDENTITY_PATH = PROMPTS_DIR / "identity.md"
PLATFORM_POLICY_PATH = PROMPTS_DIR / "platform_policy.md"
TOOL_CONTRACT_PATH = PROMPTS_DIR / "tool_contract.md"

# Bootstrap files (loaded from workspace root, not prompts/)
BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]

# Built-in default templates (for template detection)
_BUILTIN_TEMPLATES: dict[str, str] = {}


class PromptLoadError(RuntimeError):
    """Raised when a required prompt file is missing, empty or unreadable."""


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_text(path: Path, description: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptLoadError(
            f"无法加载{description}文件：{path}\n"
            f"请确认该文件存在且可读。原始错误：{exc}"
        ) from exc
    if not text:
        raise PromptLoadError(f"{description}文件内容为空：{path}")
    return text


def _load_optional(path: Path) -> str:
    """Load a file, returning '' if missing."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_builtin_template(filename: str) -> str:
    """Load a built-in default template from the prompts directory."""
    if filename not in _BUILTIN_TEMPLATES:
        path = PROMPTS_DIR / filename
        _BUILTIN_TEMPLATES[filename] = _load_optional(path)
    return _BUILTIN_TEMPLATES[filename]


# ---------------------------------------------------------------------------
# Template detection
# ---------------------------------------------------------------------------


def is_default_template(content: str, template_filename: str) -> bool:
    """Return True if *content* matches the built-in default template.

    When the user hasn't customized a file (SOUL.md, USER.md, MEMORY.md),
    we should skip injecting it into context to save tokens.
    """
    default = _load_builtin_template(template_filename)
    if not default:
        return False
    # Compare after normalizing whitespace
    return content.strip() == default.strip()


# ---------------------------------------------------------------------------
# Identity (rendered template)
# ---------------------------------------------------------------------------


def build_identity(
    workspace_path: str = "",
    channel: str = "",
    timezone: str | None = None,
) -> str:
    """Render the identity template with runtime variables."""
    system = _platform.system()
    runtime = (
        f"{'macOS' if system == 'Darwin' else system} "
        f"{_platform.machine()}, Python {_platform.python_version()}"
    )

    platform_policy = render_file(
        PLATFORM_POLICY_PATH,
        {"system": system},
    ) if PLATFORM_POLICY_PATH.exists() else ""

    return render_file(
        IDENTITY_PATH,
        {
            "runtime": runtime,
            "workspace_path": workspace_path or str(PROJECT_ROOT),
            "platform_policy": platform_policy,
            "channel": channel,
            "timezone": timezone or "",
        },
    ) if IDENTITY_PATH.exists() else _build_fallback_identity(workspace_path, system)


def _build_fallback_identity(workspace_path: str, system: str) -> str:
    """Fallback identity when identity.md is missing."""
    runtime = (
        f"{'macOS' if system == 'Darwin' else system} "
        f"{_platform.machine()}, Python {_platform.python_version()}"
    )
    ws = workspace_path or "."
    return (
        f"你是一个 AI 助手（claw），运行在本地工作区内。\n"
        f"工作区路径: {ws}\n"
        f"运行环境: {runtime}\n\n"
        f"始终以中文回复用户，除非用户明确要求其他语言。"
    )


# ---------------------------------------------------------------------------
# Tool contract
# ---------------------------------------------------------------------------


def load_tool_contract() -> str:
    """Load the tool contract template."""
    return _load_text(TOOL_CONTRACT_PATH, "工具契约")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def load_system_prompt() -> str:
    """Load the system prompt from `prompts/system_prompt.md`.

    This is the base system prompt — it is NOT the identity template.
    It provides core behavioral rules that are stable across sessions.
    """
    return _load_text(SYSTEM_PROMPT_PATH, "system prompt")


# ---------------------------------------------------------------------------
# Soul
# ---------------------------------------------------------------------------


def load_soul() -> str:
    """Load the soul/personality from `prompts/soul.md`."""
    return _load_text(SOUL_PATH, "soul")


def load_soul_if_customized() -> str | None:
    """Return the soul content only if it differs from the default template."""
    content = _load_optional(SOUL_PATH)
    if not content or is_default_template(content, "soul.md"):
        return None
    return content


# ---------------------------------------------------------------------------
# Bootstrap files (workspace root)
# ---------------------------------------------------------------------------


def load_bootstrap_files(workspace_root: Path | None = None) -> dict[str, str]:
    """Load AGENTS.md, SOUL.md, USER.md from the workspace root.

    Returns a dict of ``{filename: content}`` for files that exist.
    """
    root = workspace_root or PROJECT_ROOT
    result: dict[str, str] = {}
    for filename in BOOTSTRAP_FILES:
        file_path = root / filename
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    result[filename] = content
            except OSError:
                pass
    return result


def load_user_profile(workspace_root: Path | None = None) -> str | None:
    """Load USER.md from workspace root, only if customized."""
    root = workspace_root or PROJECT_ROOT
    path = root / "USER.md"
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content or is_default_template(content, "USER.md"):
        return None
    return content


# ---------------------------------------------------------------------------
# Hot-reload
# ---------------------------------------------------------------------------

# Mutable cache for hot-reloaded content
_hot_system_prompt: str | None = None
_hot_soul: str | None = None


def set_hot_system_prompt(content: str) -> None:
    global _hot_system_prompt
    _hot_system_prompt = content


def set_hot_soul(content: str) -> None:
    global _hot_soul
    _hot_soul = content


def get_system_prompt() -> str:
    """Get the effective system prompt (hot-reload aware)."""
    if _hot_system_prompt is not None:
        return _hot_system_prompt
    return load_system_prompt()


def get_soul() -> str:
    """Get the effective soul (hot-reload aware)."""
    if _hot_soul is not None:
        return _hot_soul
    return load_soul()
