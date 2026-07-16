"""Configuration loading for claw.

Reads LLM configuration from a `.env` file (project root) or from the
system environment. Real secrets must never be hard-coded or committed;
see `.env.example` for the required keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from claw.paths import data_dir, env_example_path, env_path, resource_root

# Source checkout root in development; bundled resource root in packaged builds.
PROJECT_ROOT = resource_root()
ENV_PATH = env_path()
ENV_EXAMPLE_PATH = env_example_path()

# Runtime data (sessions, memory, ...). Entirely gitignored.
DATA_DIR = data_dir()
SESSIONS_DIR = DATA_DIR / "sessions"
MEMORY_DIR = DATA_DIR / "memory"

_REQUIRED_VARS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

# Context window defaults
_DEFAULT_CONTEXT_WINDOW = 32000
_DEFAULT_CONTEXT_USAGE_RATIO = 0.80

# Compaction message-token thresholds (env-overridable)
_DEFAULT_MAX_MESSAGE_TOKENS = 2000
_DEFAULT_KEEP_RECENT_TOKENS = 1000
_DEFAULT_KEEP_RECENT_MESSAGES_MIN = 4

# Consolidation defaults
_DEFAULT_CONSOLIDATION_RATIO = 0.5
_DEFAULT_MAX_OUTPUT_TOKENS = 4096

# Idle compaction TTL: only compact sessions idle longer than this
# (minutes).  Default 60 min (1 hour).  Set to 0 to disable.
_DEFAULT_IDLE_TTL_MINUTES = 60

# History log
_DEFAULT_MAX_HISTORY_ENTRIES = 2000

# Heartbeat defaults
_DEFAULT_HEARTBEAT_ENABLED = True
_DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60  # 30 minutes
_DEFAULT_HEARTBEAT_KEEP_RECENT = 8


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid.

    The message is meant to be shown directly to the user, so it must
    stay clear and actionable instead of leaking a raw stack trace.
    """


# ---------------------------------------------------------------------------
# .env loading (called once, shared across all config loaders)
# ---------------------------------------------------------------------------

_dotenv_loaded: bool = False


def _ensure_dotenv_loaded() -> None:
    """Load ``.env`` once into the process environment.

    ``load_dotenv(override=False)`` is idempotent, but repeating the
    file read + parse on every ``load_config()`` / ``load_compaction_config()``
    / ``load_heartbeat_config()`` call is wasteful.  This guard ensures
    the file is only read once per process lifetime.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    # Try UTF-8 first, fall back to GBK (common on Chinese Windows)
    try:
        load_dotenv(dotenv_path=ENV_PATH, override=False, encoding="utf-8")
    except (UnicodeDecodeError, LookupError):
        try:
            load_dotenv(dotenv_path=ENV_PATH, override=False, encoding="gbk")
        except Exception:
            pass  # .env not required if env vars are already set


@dataclass(frozen=True)
class LLMConfig:
    """Resolved configuration needed to talk to the LLM API."""

    api_key: str
    base_url: str
    model: str
    context_window: int = _DEFAULT_CONTEXT_WINDOW
    context_usage_ratio: float = _DEFAULT_CONTEXT_USAGE_RATIO
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS
    consolidation_ratio: float = _DEFAULT_CONSOLIDATION_RATIO

    @property
    def max_context_tokens(self) -> int:
        """Effective token budget: window × usage_ratio."""
        return int(self.context_window * self.context_usage_ratio)


@dataclass(frozen=True)
class CompactionConfig:
    """Configuration for the compaction subsystem.

    When *api_key* / *base_url* / *model* are empty, the caller should
    fall back to the main ``LLMConfig`` values.
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    max_message_tokens: int = _DEFAULT_MAX_MESSAGE_TOKENS
    keep_recent_tokens: int = _DEFAULT_KEEP_RECENT_TOKENS
    keep_recent_messages_min: int = _DEFAULT_KEEP_RECENT_MESSAGES_MIN
    idle_ttl_minutes: int = _DEFAULT_IDLE_TTL_MINUTES
    max_history_entries: int = _DEFAULT_MAX_HISTORY_ENTRIES


@dataclass(frozen=True)
class HeartbeatConfig:
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


def load_config() -> LLMConfig:
    """Load LLM configuration from `.env` or the system environment.

    Environment variables that are already set take precedence over
    values found in `.env`.

    Raises:
        ConfigError: if one or more required variables are missing or
            blank.
    """
    _ensure_dotenv_loaded()
    from claw.runtime_settings import setting_value

    values = {name: setting_value(name, "").strip() for name in _REQUIRED_VARS}
    missing = [name for name, value in values.items() if not value]

    if missing:
        raise ConfigError(_missing_config_message(missing))

    context_window_str = setting_value("LLM_CONTEXT_WINDOW", "").strip()
    try:
        context_window = int(context_window_str) if context_window_str else _DEFAULT_CONTEXT_WINDOW
    except ValueError:
        context_window = _DEFAULT_CONTEXT_WINDOW

    usage_ratio_str = setting_value("LLM_CONTEXT_USAGE_RATIO", "").strip()
    try:
        context_usage_ratio = float(usage_ratio_str) if usage_ratio_str else _DEFAULT_CONTEXT_USAGE_RATIO
    except ValueError:
        context_usage_ratio = _DEFAULT_CONTEXT_USAGE_RATIO

    max_output_str = setting_value("LLM_MAX_OUTPUT_TOKENS", "").strip()
    try:
        max_output_tokens = int(max_output_str) if max_output_str else _DEFAULT_MAX_OUTPUT_TOKENS
    except ValueError:
        max_output_tokens = _DEFAULT_MAX_OUTPUT_TOKENS

    consolidation_ratio_str = setting_value("LLM_CONSOLIDATION_RATIO", "").strip()
    try:
        consolidation_ratio = float(consolidation_ratio_str) if consolidation_ratio_str else _DEFAULT_CONSOLIDATION_RATIO
    except ValueError:
        consolidation_ratio = _DEFAULT_CONSOLIDATION_RATIO

    return LLMConfig(
        api_key=values["LLM_API_KEY"],
        base_url=values["LLM_BASE_URL"],
        model=values["LLM_MODEL"],
        context_window=context_window,
        context_usage_ratio=context_usage_ratio,
        max_output_tokens=max_output_tokens,
        consolidation_ratio=consolidation_ratio,
    )


def load_compaction_config() -> CompactionConfig:
    """Load compaction-specific configuration from the environment.

    All fields are optional — when the compaction LLM credentials are
    left blank the caller reuses the main ``LLMConfig``.
    """
    _ensure_dotenv_loaded()

    def _int_env(name: str, default: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return CompactionConfig(
        api_key=os.getenv("COMPACT_LLM_API_KEY", "").strip(),
        base_url=os.getenv("COMPACT_LLM_BASE_URL", "").strip(),
        model=os.getenv("COMPACT_LLM_MODEL", "").strip(),
        max_message_tokens=_int_env("COMPACT_MAX_MESSAGE_TOKENS", _DEFAULT_MAX_MESSAGE_TOKENS),
        keep_recent_tokens=_int_env("COMPACT_KEEP_RECENT_TOKENS", _DEFAULT_KEEP_RECENT_TOKENS),
        keep_recent_messages_min=_int_env("COMPACT_KEEP_RECENT_MESSAGES_MIN", _DEFAULT_KEEP_RECENT_MESSAGES_MIN),
        idle_ttl_minutes=_int_env("COMPACT_IDLE_TTL_MINUTES", _DEFAULT_IDLE_TTL_MINUTES),
        max_history_entries=_int_env("HISTORY_MAX_ENTRIES", _DEFAULT_MAX_HISTORY_ENTRIES),
    )


def load_heartbeat_config() -> HeartbeatConfig:
    """Load Heartbeat configuration from environment."""
    _ensure_dotenv_loaded()

    def _int_env(name: str, default: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    enabled_str = os.getenv("HEARTBEAT_ENABLED", "").strip().lower()
    enabled = enabled_str != "false" if enabled_str else _DEFAULT_HEARTBEAT_ENABLED

    return HeartbeatConfig(
        enabled=enabled,
        interval_s=_int_env("HEARTBEAT_INTERVAL_S", _DEFAULT_HEARTBEAT_INTERVAL_S),
        keep_recent_messages=_int_env("HEARTBEAT_KEEP_RECENT", _DEFAULT_HEARTBEAT_KEEP_RECENT),
    )


# ---------------------------------------------------------------------------
# QQ channel config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QQChannelConfig:
    """Configuration for the QQ bot channel.

    Uses the Official QQ Bot API v2 with WebSocket Gateway.
    Credentials can be obtained by scanning a QR code:
        python -m claw.channels.qq_onboard
    """

    enabled: bool = False
    app_id: str = ""
    client_secret: str = ""
    allow_from: list[str] = field(default_factory=list)
    markdown_support: bool = True
    ack_message: str = ""


def load_qq_config() -> QQChannelConfig:
    """Load QQ channel configuration from environment variables."""
    _ensure_dotenv_loaded()
    from claw.runtime_settings import setting_value

    enabled = setting_value("QQ_ENABLED", "false").strip().lower() in ("true", "1", "yes")
    app_id = setting_value("QQ_APP_ID", "").strip()
    client_secret = setting_value("QQ_CLIENT_SECRET", "").strip()
    allow_from_raw = setting_value("QQ_ALLOW_FROM", "").strip()
    allow_from = [u.strip() for u in allow_from_raw.split(",") if u.strip()] if allow_from_raw else []
    markdown_support = setting_value("QQ_MSG_FORMAT", "markdown").strip() == "markdown"
    ack_message = setting_value("QQ_ACK_MESSAGE", "").strip()

    return QQChannelConfig(
        enabled=enabled,
        app_id=app_id,
        client_secret=client_secret,
        allow_from=allow_from,
        markdown_support=markdown_support,
        ack_message=ack_message,
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
