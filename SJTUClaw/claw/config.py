"""Configuration loading for claw.

Reads LLM configuration from a `.env` file (project root) or from the
system environment. Real secrets must never be hard-coded or committed;
see `.env.example` for the required keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# claw/config.py -> claw/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

# Runtime data (sessions, memory, ...). Entirely gitignored.
DATA_DIR = PROJECT_ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
MEMORY_FILE = DATA_DIR / "memory" / "memory.json"

_REQUIRED_VARS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid.

    The message is meant to be shown directly to the user, so it must
    stay clear and actionable instead of leaking a raw stack trace.
    """


@dataclass(frozen=True)
class LLMConfig:
    """Resolved configuration needed to talk to the LLM API."""

    api_key: str
    base_url: str
    model: str


def load_config() -> LLMConfig:
    """Load LLM configuration from `.env` or the system environment.

    Environment variables that are already set take precedence over
    values found in `.env`.

    Raises:
        ConfigError: if one or more required variables are missing or
            blank.
    """
    load_dotenv(dotenv_path=ENV_PATH, override=False)

    values = {name: os.getenv(name, "").strip() for name in _REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]

    if missing:
        raise ConfigError(_missing_config_message(missing))

    return LLMConfig(
        api_key=values["LLM_API_KEY"],
        base_url=values["LLM_BASE_URL"],
        model=values["LLM_MODEL"],
    )


def _missing_config_message(missing: list[str]) -> str:
    missing_list = "\n".join(f"  - {name}" for name in missing)
    return (
        "缺少必要的配置项，无法启动 claw：\n"
        f"{missing_list}\n\n"
        "请执行以下任一操作：\n"
        f"  1. 复制 {ENV_EXAMPLE_PATH.name} 为 {ENV_PATH.name}"
        f"（路径：{ENV_PATH}），并填写上述配置项；\n"
        "  2. 或直接在系统环境变量中设置上述配置项。\n\n"
        "提示：LLM_API_KEY 是访问模型服务的密钥，LLM_BASE_URL 是服务地址，"
        "LLM_MODEL 是要使用的模型名称。"
    )
