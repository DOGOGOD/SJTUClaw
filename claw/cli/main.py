"""SJTUClaw CLI — main entry point.

Usage:
    sjtuclaw gateway              Start the HTTP + WebSocket gateway
    sjtuclaw setup                Interactive setup wizard
    sjtuclaw chat                 Start interactive CLI chat (default)

Follows the CLI structure: ``sjtuclaw gateway``, ``sjtuclaw setup``, etc.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_ENV_EXAMPLE = _PROJECT_ROOT / ".env.example"


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _read_env() -> dict[str, str]:
    """Read .env into a dict, preserving order. Falls back to .env.example."""
    path = _ENV_PATH if _ENV_PATH.exists() else _ENV_EXAMPLE
    result: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _, val = stripped.partition("=")
                result[key.strip()] = val.strip()
    return result


def _write_env(updates: dict[str, str]) -> None:
    """Write or update key-value pairs in .env, preserving other content."""
    _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
    elif _ENV_EXAMPLE.exists():
        lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updated: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        matched = False
        for key, val in updates.items():
            if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}=") or stripped.startswith(f"#{key}="):
                new_lines.append(f"{key}={val}")
                updated.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in updated:
            new_lines.append(f"{key}={val}")

    _ENV_PATH.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")


def _prompt_yn(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question. Returns True for yes."""
    hint = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + hint).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _prompt_str(prompt: str, default: str = "") -> str:
    """Ask for a string value."""
    hint = f" [{default}]: " if default else ": "
    raw = input(prompt + hint).strip()
    return raw if raw else default


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------

def _setup_llm() -> dict[str, str]:
    """Configure LLM credentials interactively."""
    env = _read_env()

    print()
    print("─" * 48)
    print("  LLM 配置")
    print("─" * 48)

    current_key = env.get("LLM_API_KEY", "")
    current_url = env.get("LLM_BASE_URL", "")
    current_model = env.get("LLM_MODEL", "")

    if current_key and current_url and current_model:
        masked = current_key[:8] + "****" + current_key[-4:] if len(current_key) > 12 else "****"
        print(f"  当前配置:")
        print(f"    LLM_API_KEY  = {masked}")
        print(f"    LLM_BASE_URL = {current_url}")
        print(f"    LLM_MODEL    = {current_model}")
        print()
        if not _prompt_yn("  是否修改?", default=False):
            return {}

    api_key = _prompt_str("  API Key", current_key)
    base_url = _prompt_str("  Base URL", current_url or "https://api.openai.com/v1")
    model = _prompt_str("  Model", current_model or "gpt-4o")

    return {
        "LLM_API_KEY": api_key,
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": model,
    }


def _setup_qq() -> dict[str, str]:
    """Configure QQ Bot channel. Returns env updates."""
    env = _read_env()

    print()
    print("─" * 48)
    print("  QQ Bot 配置")
    print("─" * 48)

    current_app_id = env.get("QQ_APP_ID", "")
    current_secret = env.get("QQ_CLIENT_SECRET", "")
    current_enabled = env.get("QQ_ENABLED", "false").lower() == "true"

    if current_enabled and current_app_id and current_secret:
        print(f"  QQ 已配置:")
        print(f"    QQ_APP_ID        = {current_app_id}")
        print(f"    QQ_CLIENT_SECRET = ****")
        print()
        if not _prompt_yn("  是否重新配置?", default=False):
            return {}

    print()
    print("  方式 1: 扫码自动获取 (需先在另一终端运行 sjtuclaw gateway)")
    print("  方式 2: 手动输入 AppID 和 AppSecret")
    choice = _prompt_str("  选择方式", "1")

    if choice == "1":
        from claw.channels.qq_onboard import qr_register

        print()
        result = qr_register()
        if result is None:
            print("  扫码失败，请手动输入。")
            choice = "2"
        else:
            return {
                "QQ_ENABLED": "true",
                "QQ_APP_ID": result["app_id"],
                "QQ_CLIENT_SECRET": result["client_secret"],
                "QQ_ALLOW_FROM": result.get("user_openid", "*"),
            }

    if choice == "2":
        app_id = _prompt_str("  AppID", current_app_id)
        secret = _prompt_str("  AppSecret", current_secret)
        allow = _prompt_str("  AllowFrom (用户openid, * = 全部)", "*")
        return {
            "QQ_ENABLED": "true",
            "QQ_APP_ID": app_id,
            "QQ_CLIENT_SECRET": secret,
            "QQ_ALLOW_FROM": allow,
        }

    return {}


def _setup_channels() -> dict[str, str]:
    """Configure messaging channels. Returns env updates."""
    env = _read_env()
    updates: dict[str, str] = {}

    print()
    print("─" * 48)
    print("  通道配置")
    print("─" * 48)

    # Check which channels are already configured
    qq_configured = bool(env.get("QQ_APP_ID", "")) and env.get("QQ_ENABLED", "").lower() == "true"

    configured = []
    if qq_configured:
        configured.append("QQ Bot")
    if configured:
        print(f"  已配置: {', '.join(configured)}")
    else:
        print("  当前未配置任何通道。")

    print()
    if not _prompt_yn("  是否配置通道?", default=not bool(configured)):
        return updates

    # For now, only QQ is supported
    print()
    print("  可用通道:")
    status = " [已配置]" if qq_configured else ""
    print(f"    1. QQ Bot (官方 QQ Bot API v2){status}")

    choice = _prompt_str("  选择通道", "1")
    if choice == "1":
        updates.update(_setup_qq())

    # Check if more channels to configure
    if updates:
        print()
        print(f"  通道配置完成。")
    return updates


# ---------------------------------------------------------------------------
# Main setup wizard
# ---------------------------------------------------------------------------

def _cmd_setup() -> int:
    """Interactive setup wizard."""
    print()
    print("=" * 56)
    print("  SJTUClaw 配置向导")
    print("=" * 56)

    all_updates: dict[str, str] = {}

    # Step 1: LLM
    if _prompt_yn("\n  是否配置 LLM?", default=True):
        all_updates.update(_setup_llm())

    # Step 2: Channels
    all_updates.update(_setup_channels())

    # Write
    if all_updates:
        _write_env(all_updates)
        print()
        print("=" * 56)
        print("  配置已保存到 .env")
        print()
        masked_keys = {"LLM_API_KEY", "QQ_CLIENT_SECRET"}
        for k, v in all_updates.items():
            if k in masked_keys:
                print(f"  {k} = ****")
            else:
                print(f"  {k} = {v}")
        print()
        print("  运行 sjtuclaw gateway 启动。")
    else:
        print()
        print("  未做任何更改。")

    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cmd_gateway() -> int:
    """Start the gateway server."""
    from claw.gateway.__main__ import main as gateway_main
    return gateway_main()


def _cmd_chat() -> int:
    """Start interactive CLI chat."""
    from claw.main import main as chat_main
    return chat_main()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sjtuclaw",
        description="SJTUClaw — AI Agent with QQ Bot support",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    sub.add_parser("gateway", help="Start the HTTP + WebSocket gateway")
    sub.add_parser("chat",    help="Start interactive CLI chat")
    sub.add_parser("setup",   help="Interactive setup wizard")

    args = parser.parse_args()

    if args.command == "gateway":
        return _cmd_gateway()
    elif args.command == "setup":
        return _cmd_setup()
    else:
        return _cmd_chat()


if __name__ == "__main__":
    sys.exit(main())
