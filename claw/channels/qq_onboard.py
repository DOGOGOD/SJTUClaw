"""
QQBot scan-to-configure (QR code onboard) module.

Mirrors the Feishu onboarding pattern: synchronous HTTP + a single public
entry-point ``qr_register()`` that handles the full flow (create task →
display QR code → poll → decrypt credentials).

Calls the ``q.qq.com`` ``create_bind_task`` / ``poll_bind_result`` APIs to
generate a QR-code URL and poll for scan completion.  On success the caller
receives the bot's *app_id*, *client_secret* (decrypted locally), and the
scanner's *user_openid* — enough to fully configure the QQBot gateway.

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import io
import logging
import sys
import time
from enum import IntEnum
from typing import Optional, Tuple
from urllib.parse import quote

from .qq_constants import (
    ONBOARD_API_TIMEOUT,
    ONBOARD_CREATE_PATH,
    ONBOARD_POLL_INTERVAL,
    ONBOARD_POLL_PATH,
    PORTAL_HOST,
    QR_URL_TEMPLATE,
)
from .qq_crypto import decrypt_secret, generate_bind_key
from .qq_utils import get_api_headers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bind status
# ---------------------------------------------------------------------------


class BindStatus(IntEnum):
    """Status codes returned by ``_poll_bind_result``."""

    NONE = 0
    PENDING = 1
    COMPLETED = 2
    EXPIRED = 3


# ---------------------------------------------------------------------------
# QR rendering
# ---------------------------------------------------------------------------

try:
    import qrcode as _qrcode_mod
except (ImportError, TypeError):
    _qrcode_mod = None  # type: ignore[assignment]


def _render_qr(url: str) -> bool:
    """Try to render a QR code in the terminal. Returns True if successful."""
    if _qrcode_mod is None:
        return False
    try:
        qr = _qrcode_mod.QRCode(
            error_correction=_qrcode_mod.constants.ERROR_CORRECT_M,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        # On Windows GBK terminals, the QR block characters (▀ ▄ █ etc.)
        # can't be encoded, causing UnicodeEncodeError.  Force UTF-8 output
        # for the duration of the render call.
        try:
            qr.print_ascii(invert=True)
        except UnicodeEncodeError:
            _stdout = sys.stdout
            _wrapper = io.TextIOWrapper(
                _stdout.buffer, encoding="utf-8", errors="replace"
            )
            try:
                sys.stdout = _wrapper
                qr.print_ascii(invert=True)
            finally:
                sys.stdout = _stdout
                # Detach the wrapper so its __del__ doesn't close the
                # underlying buffer.  Without this, every print() after
                # _render_qr returns raises ValueError.
                _wrapper.detach()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Synchronous HTTP helpers (mirrors Feishu _post_registration pattern)
# ---------------------------------------------------------------------------


def _create_bind_task(timeout: float = ONBOARD_API_TIMEOUT) -> Tuple[str, str]:
    """Create a bind task and return *(task_id, aes_key_base64)*.

    Raises:
        RuntimeError: If the API returns a non-zero ``retcode``.
    """
    import httpx

    url = f"https://{PORTAL_HOST}{ONBOARD_CREATE_PATH}"
    key = generate_bind_key()

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, json={"key": key}, headers=get_api_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg", "create_bind_task failed"))

    task_id = data.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError("create_bind_task: missing task_id in response")

    logger.debug("create_bind_task ok: task_id=%s", task_id)
    return task_id, key


def _poll_bind_result(
    task_id: str,
    timeout: float = ONBOARD_API_TIMEOUT,
) -> Tuple[BindStatus, str, str, str]:
    """Poll the bind result for *task_id*.

    Returns:
        A 4-tuple of ``(status, bot_appid, bot_encrypt_secret, user_openid)``.

    Raises:
        RuntimeError: If the API returns a non-zero ``retcode``.
    """
    import httpx

    url = f"https://{PORTAL_HOST}{ONBOARD_POLL_PATH}"

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, json={"task_id": task_id}, headers=get_api_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg", "poll_bind_result failed"))

    d = data.get("data", {})
    return (
        BindStatus(d.get("status", 0)),
        str(d.get("bot_appid", "")),
        d.get("bot_encrypt_secret", ""),
        d.get("user_openid", ""),
    )


def build_connect_url(task_id: str) -> str:
    """Build the QR-code target URL for a given *task_id*."""
    return QR_URL_TEMPLATE.format(task_id=quote(task_id))


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

_MAX_REFRESHES = 3


def qr_register(timeout_seconds: int = 600) -> Optional[dict]:
    """Run the QQBot scan-to-configure QR registration flow.

    Mirrors ``feishu.qr_register()``: handles create → display → poll →
    decrypt in one call.  Unexpected errors propagate to the caller.

    :returns:
        ``{"app_id": ..., "client_secret": ..., "user_openid": ...}`` on
        success, or ``None`` on failure / expiry / cancellation.
    """
    deadline = time.monotonic() + timeout_seconds

    for refresh_count in range(_MAX_REFRESHES + 1):
        # ---- Create bind task ----
        try:
            task_id, aes_key = _create_bind_task()
        except Exception as exc:
            logger.warning("[QQBot onboard] Failed to create bind task: %s", exc)
            return None

        url = build_connect_url(task_id)

        # ---- Display QR code + URL ----
        print()
        if _render_qr(url):
            print(f"  Scan the QR code above, or open this URL directly:\n  {url}")
        else:
            print(f"  Open this URL in QQ on your phone:\n  {url}")
            print("  Tip: pip install qrcode  to display a scannable QR code here")
        print()
        print("  等待手机扫码确认", end="", flush=True)

        # ---- Poll loop ----
        _poll_count = 0
        while time.monotonic() < deadline:
            try:
                status, app_id, encrypted_secret, user_openid = _poll_bind_result(task_id)
            except Exception:
                time.sleep(ONBOARD_POLL_INTERVAL)
                _poll_count += 1
                if _poll_count % 10 == 0:
                    print(".", end="", flush=True)
                continue

            _poll_count += 1
            if _poll_count % 5 == 0:
                print(".", end="", flush=True)

            if status == BindStatus.COMPLETED:
                client_secret = decrypt_secret(encrypted_secret, aes_key)
                print()
                print(f"  QR scan complete! (App ID: {app_id})")
                if user_openid:
                    print(f"  Scanner's OpenID: {user_openid}")
                return {
                    "app_id": app_id,
                    "client_secret": client_secret,
                    "user_openid": user_openid,
                }

            if status == BindStatus.EXPIRED:
                if refresh_count >= _MAX_REFRESHES:
                    logger.warning("[QQBot onboard] QR code expired %d times -- giving up", _MAX_REFRESHES)
                    return None
                print(f"\n  QR code expired, refreshing... ({refresh_count + 1}/{_MAX_REFRESHES})")
                break  # next for-loop iteration creates a new task

            time.sleep(ONBOARD_POLL_INTERVAL)
        else:
            # deadline reached without completing
            logger.warning("[QQBot onboard] Poll timed out after %ds", timeout_seconds)
            return None

    return None


# ---------------------------------------------------------------------------
# CLI entry point -- ``python -m claw.channels.qq_onboard``
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from pathlib import Path as _Path

    _env_path = _Path(__file__).resolve().parent.parent.parent / ".env"

    print("=" * 56)
    print("  QQ Bot 配置")
    print("=" * 56)
    print()
    print("  方式一（推荐）：手动配置")
    print("    1. 前往 https://q.qq.com 注册并创建机器人应用")
    print("    2. 在开发设置中获取 AppID 和 AppSecret")
    print("    3. 写入 .env:")
    print("       QQ_ENABLED=true")
    print("       QQ_APP_ID=你的AppID")
    print("       QQ_CLIENT_SECRET=你的AppSecret")
    print("       QQ_ALLOW_FROM=*")
    print()
    print("  方式二：扫码自动获取（可能不稳定）")
    print("    即将生成二维码，请用手机 QQ 扫描并在手机上确认。")
    print()

    result = qr_register()

    if result is None:
        print()
        print("  扫码未成功。请使用方式一手动配置。")
        print(f"  配置后写入: {_env_path}")
        sys.exit(1)

    # Write to .env
    _env_path.parent.mkdir(parents=True, exist_ok=True)

    if _env_path.exists():
        _lines = _env_path.read_text(encoding="utf-8").splitlines()
    else:
        _example = _env_path.parent / ".env.example"
        if _example.exists():
            _lines = _example.read_text(encoding="utf-8").splitlines()
        else:
            _lines = []

    _keys = {
        "QQ_ENABLED": "true",
        "QQ_APP_ID": result["app_id"],
        "QQ_CLIENT_SECRET": result["client_secret"],
    }
    if result.get("user_openid"):
        _keys["QQ_ALLOW_FROM"] = result["user_openid"]

    _updated: set[str] = set()
    _new_lines: list[str] = []
    for _line in _lines:
        _stripped = _line.strip()
        _matched = False
        for _key, _val in _keys.items():
            if _stripped.startswith(f"{_key}=") or _stripped.startswith(f"# {_key}=") or _stripped.startswith(f"#{_key}="):
                _new_lines.append(f"{_key}={_val}")
                _updated.add(_key)
                _matched = True
                break
        if not _matched:
            _new_lines.append(_line)

    for _key, _val in _keys.items():
        if _key not in _updated:
            _new_lines.append(f"{_key}={_val}")

    _env_path.write_text("\n".join(_new_lines).strip() + "\n", encoding="utf-8")

    print()
    print(f"  配置已写入: {_env_path}")
    print(f"    QQ_ENABLED=true")
    print(f"    QQ_APP_ID={result['app_id']}")
    print(f"    QQ_CLIENT_SECRET=****")
    if result.get("user_openid"):
        print(f"    QQ_ALLOW_FROM={result['user_openid']}")
    print()
    print("  配置完成！运行 python -m claw.gateway 启动。")
    sys.exit(0)
