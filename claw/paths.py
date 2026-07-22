"""Runtime paths for source and packaged SJTUClaw builds."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


USER_DIR_NAME = ".sjtuclaw"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Directory containing bundled read-only application resources."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parent.parent


def user_root() -> Path:
    """Writable per-user application directory."""
    override = os.getenv("SJTUCLAW_USER_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / USER_DIR_NAME


def main_dir() -> Path:
    """Default directory exposed to the agent when no workspace is selected.

    Source runs should behave as if they were started from the checkout root,
    regardless of the shell directory used to launch the Gateway.  A packaged
    desktop build instead gets a stable, writable home under ``.sjtuclaw``.
    """
    if is_frozen():
        return user_root()
    return resource_root()


def data_dir() -> Path:
    override = os.getenv("SJTUCLAW_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        return user_root() / "data"
    return resource_root() / "data"


def env_path() -> Path:
    if is_frozen():
        return user_root() / ".env"
    return resource_root() / ".env"


def env_example_path() -> Path:
    return resource_root() / ".env.example"


def web_dir() -> Path:
    return resource_root() / "web"


def prompts_dir() -> Path:
    if not is_frozen():
        return bundled_prompts_dir()
    target = data_dir() / "prompts"
    source = bundled_prompts_dir()
    if source.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    return target


def bundled_prompts_dir() -> Path:
    return resource_root() / "prompts"


def bundled_skills_dir() -> Path:
    return resource_root() / "skills"


def skills_dir() -> Path:
    if not is_frozen():
        return bundled_skills_dir()
    target = data_dir() / "skills"
    source = bundled_skills_dir()
    if source.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    return target


def pet_assets_dir() -> Path:
    frozen_assets = resource_root() / "claw" / "pet" / "assets"
    if frozen_assets.exists():
        return frozen_assets
    return Path(__file__).resolve().parent / "pet" / "assets"
