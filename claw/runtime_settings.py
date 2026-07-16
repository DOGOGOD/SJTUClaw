"""Encrypted runtime settings used by the WebUI.

The project still accepts ``.env`` as a bootstrap source, but WebUI edits are
persisted here so secrets do not have to be written back to plaintext files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from claw.paths import data_dir, resource_root

PROJECT_ROOT = resource_root()
DATA_DIR = data_dir()
SETTINGS_DIR = DATA_DIR / "settings"
SETTINGS_PATH = SETTINGS_DIR / "runtime_settings.json"
KEY_PATH = SETTINGS_DIR / "runtime_settings.key"

SECRET_KEYS = {
    "LLM_API_KEY",
    "COMPACT_LLM_API_KEY",
    "QQ_CLIENT_SECRET",
}


def _fernet() -> Fernet:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        key = KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        KEY_PATH.write_bytes(key)
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(key)


def _encrypt(value: str) -> dict[str, str]:
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return {"encrypted": token}


def _decrypt(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not value.get("encrypted"):
        return ""
    try:
        return _fernet().decrypt(str(value["encrypted"]).encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def load_runtime_settings(decrypt_secrets: bool = True) -> dict[str, str]:
    """Return all persisted WebUI settings as flat environment-style keys."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    result: dict[str, str] = {}
    for key, value in raw.items():
        if key in SECRET_KEYS:
            result[key] = _decrypt(value) if decrypt_secrets else ("********" if _decrypt(value) else "")
        elif value is None:
            result[key] = ""
        else:
            result[key] = str(value)
    return result


def update_runtime_settings(updates: dict[str, Any]) -> dict[str, str]:
    """Merge settings into the encrypted runtime settings file."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    current = load_runtime_settings_raw()

    for key, value in updates.items():
        normalized = "" if value is None else str(value).strip()
        if key in SECRET_KEYS:
            if normalized:
                current[key] = _encrypt(normalized)
        else:
            current[key] = normalized

    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return load_runtime_settings(decrypt_secrets=True)


def load_runtime_settings_raw() -> dict[str, Any]:
    """Read the encrypted settings payload without decrypting it."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def replace_runtime_settings_raw(payload: dict[str, Any]) -> None:
    """Replace the encrypted settings payload exactly, for rollback."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def setting_value(name: str, default: str = "") -> str:
    """Read one effective value, preferring encrypted WebUI settings."""
    runtime = load_runtime_settings(decrypt_secrets=True)
    if name in runtime:
        return runtime[name]
    return os.getenv(name, default)
