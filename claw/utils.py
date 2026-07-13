"""Shared utilities for the claw agent framework.

Small, dependency-free helpers that are used across multiple modules.
"""

from __future__ import annotations

import locale
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


__all__ = ["now_iso", "atomic_write", "decode_subprocess_output", "force_utf8_stdio"]
