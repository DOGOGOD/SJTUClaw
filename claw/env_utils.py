"""Small, dependency-free helpers for robust environment configuration."""

from __future__ import annotations

import math
import os


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer, falling back when it is malformed or out of range."""
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read a finite float, falling back when malformed or out of range."""
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


__all__ = ["env_float", "env_int"]
