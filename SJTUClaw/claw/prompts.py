"""Loading of stable, file-backed prompt content (system prompt & soul).

`system_prompt.md` and `soul.md` define claw's rules and persona. They
must never be hard-coded in Python, and must never be overwritten by
ordinary user messages; the only way to change them is to edit these
files and restart the program.
"""

from __future__ import annotations

from pathlib import Path

from claw.config import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.md"
SOUL_PATH = PROMPTS_DIR / "soul.md"


class PromptLoadError(RuntimeError):
    """Raised when a required prompt file is missing, empty or unreadable."""


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


def load_system_prompt() -> str:
    """Load the system prompt text from `prompts/system_prompt.md`."""
    return _load_text(SYSTEM_PROMPT_PATH, "system prompt")


def load_soul() -> str:
    """Load the soul text from `prompts/soul.md`."""
    return _load_text(SOUL_PATH, "soul")
