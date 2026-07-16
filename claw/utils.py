"""Shared utilities for the claw agent framework.

Small, dependency-free helpers that are used across multiple modules.
"""

from __future__ import annotations

import locale
import os
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


FALLBACK_TIMEZONE = "Asia/Shanghai"


def _valid_timezone_name(name: str | None) -> str | None:
    candidate = (name or "").strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except Exception:
        return None
    return candidate


def _timezone_from_tzlocal() -> str | None:
    try:
        from tzlocal import get_localzone_name
    except Exception:
        return None
    try:
        return _valid_timezone_name(get_localzone_name())
    except Exception:
        return None


def _timezone_from_localtime_symlink() -> str | None:
    if os.name == "nt":
        return None
    try:
        target = Path("/etc/localtime").resolve()
    except OSError:
        return None
    marker = "zoneinfo"
    parts = target.parts
    if marker not in parts:
        return None
    idx = parts.index(marker)
    return _valid_timezone_name("/".join(parts[idx + 1:]))


@lru_cache(maxsize=1)
def detect_system_timezone() -> str:
    """Best-effort IANA timezone detection for user-facing wall-clock time."""
    # Timezone detection can run during module import, before the normal config
    # loaders are called. Load .env here so CLAW_TIMEZONE has identical
    # semantics in CLI, gateway, tools, and background services.
    try:
        from claw.config import _ensure_dotenv_loaded

        _ensure_dotenv_loaded()
    except Exception:
        pass
    return (
        _valid_timezone_name(os.getenv("CLAW_TIMEZONE"))
        or _timezone_from_tzlocal()
        or _valid_timezone_name(os.getenv("TZ"))
        or _timezone_from_localtime_symlink()
        or FALLBACK_TIMEZONE
    )


def default_timezone_name() -> str:
    """Return the effective user-facing timezone name."""
    return detect_system_timezone()


DEFAULT_TIMEZONE = default_timezone_name()


def default_tz() -> ZoneInfo:
    """Return the default user-facing timezone."""
    return ZoneInfo(default_timezone_name())


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_local_iso(tz_name: str | None = None) -> str:
    """Return the current user-facing time in an IANA timezone."""
    return datetime.now(ZoneInfo(tz_name or default_timezone_name())).isoformat(timespec="seconds")


def atomic_write(path: Path, content: str | bytes) -> None:
    """Write *content* to *path* atomically via a temp file + replace.

    Creates parent directories as needed.  If *content* is a ``str`` it is
    written with UTF-8 encoding; ``bytes`` are written as-is.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(content, str):
        tmp.write_text(content, encoding="utf-8")
    else:
        tmp.write_bytes(content)
    tmp.replace(path)


def decode_subprocess_output(data: bytes | str | None) -> str:
    """Decode subprocess output without corrupting Windows-localized text.

    UTF-8 is the project-wide wire/storage encoding.  Native Windows tools may
    nevertheless emit the active ANSI/OEM code page (commonly GBK/CP936), so
    strict UTF-8 is attempted first and platform encodings are used as
    fallbacks.  Replacement characters are introduced only as a last resort.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data

    candidates: list[str] = ["utf-8"]
    for encoding in (
        locale.getpreferredencoding(False),
        getattr(sys.stdout, "encoding", None),
        "mbcs" if os.name == "nt" else None,
        "gb18030" if os.name == "nt" else None,
    ):
        if encoding and encoding.lower() not in {item.lower() for item in candidates}:
            candidates.append(encoding)

    for encoding in candidates:
        try:
            return data.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def force_utf8_stdio() -> None:
    """Make Unicode console output reliable across Windows code pages."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


__all__ = [
    "DEFAULT_TIMEZONE",
    "FALLBACK_TIMEZONE",
    "detect_system_timezone",
    "default_timezone_name",
    "default_tz",
    "now_iso",
    "now_local_iso",
    "atomic_write",
    "decode_subprocess_output",
    "force_utf8_stdio",
]
