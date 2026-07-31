"""SJTUClaw CLI — main entry point.

Usage:
    sjtuclaw gateway              Start the HTTP + WebSocket gateway
    sjtuclaw setup                Interactive setup wizard
    sjtuclaw tui                  Start the full-screen terminal UI
    sjtuclaw chat                 Start interactive CLI chat
    sjtuclaw desktop              Start the local Gateway and desktop window

Follows the CLI structure: ``sjtuclaw gateway``, ``sjtuclaw setup``, etc.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import math
import secrets
import socket
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from claw.config import ENV_EXAMPLE_PATH, ENV_PATH, PROJECT_ROOT
from claw.utils import force_utf8_stdio


_PROJECT_ROOT = PROJECT_ROOT
_ENV_PATH = ENV_PATH
_ENV_EXAMPLE = ENV_EXAMPLE_PATH


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


def _prompt_secret(prompt: str, current: str = "") -> str:
    """Ask for a secret without echoing or exposing its current value."""
    hint = " [回车保留现有值]: " if current else ": "
    raw = getpass.getpass(prompt + hint).strip()
    return raw if raw else current


def _prompt_int(
    prompt: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Ask for an integer and keep prompting until it is in range."""
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = minimum - 1
        if value >= minimum and (maximum is None or value <= maximum):
            return value
        if maximum is None:
            print(f"  请输入不小于 {minimum} 的整数。")
        else:
            print(f"  请输入 {minimum}～{maximum} 之间的整数。")


def _prompt_float(prompt: str, default: float, *, minimum: float) -> float:
    """Ask for a finite float greater than or equal to *minimum*."""
    while True:
        raw = input(f"{prompt} [{default:g}]: ").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            value = minimum - 1
        if math.isfinite(value) and value >= minimum:
            return value
        print(f"  请输入不小于 {minimum:g} 的数字。")


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _mask_secret(value: str) -> str:
    if len(value) <= 12:
        return "****"
    return f"{value[:8]}****{value[-4:]}"


def _prompt_timezone(current: str) -> str:
    """Ask for an IANA timezone, accepting ``auto`` to clear an override."""
    default = current or "auto"
    while True:
        raw = _prompt_str("  时区（IANA 名称，auto = 自动识别）", default).strip()
        if raw.lower() == "auto":
            return ""
        try:
            ZoneInfo(raw)
        except Exception:
            print("  无法识别该时区，例如可填写 Asia/Shanghai 或 America/New_York。")
            continue
        return raw


def _suggest_lan_origin(port: int) -> str:
    """Best-effort suggestion for the browser origin used on the local LAN."""
    candidates: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            parsed = ipaddress.ip_address(address)
            if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
                candidates.add(address)
    except (OSError, ValueError):
        return ""

    def _rank(address: str) -> tuple[int, str]:
        if address.startswith("192.168."):
            return 0, address
        if address.startswith("10."):
            return 1, address
        return 2, address

    if not candidates:
        return ""
    return f"http://{sorted(candidates, key=_rank)[0]}:{port}"


def _valid_origins(value: str) -> bool:
    """Validate a comma-separated list of exact HTTP(S) browser origins."""
    if not value.strip():
        return False
    for item in value.split(","):
        parsed = urlsplit(item.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return False
    return True


def _prompt_origins(current: str, port: int) -> str:
    default = current or _suggest_lan_origin(port)
    while True:
        value = _prompt_str(
            "  允许的 Web 来源（逗号分隔，如 http://192.168.1.10:8000）",
            default,
        )
        if _valid_origins(value):
            return ",".join(item.strip().rstrip("/") for item in value.split(","))
        print("  局域网 Web UI 至少需要一个完整的 http:// 或 https:// 来源地址。")


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
        print("  当前配置:")
        print(f"    LLM_API_KEY  = {_mask_secret(current_key)}")
        print(f"    LLM_BASE_URL = {current_url}")
        print(f"    LLM_MODEL    = {current_model}")
        print()
        if not _prompt_yn("  是否修改?", default=False):
            return {}

    api_key = _prompt_secret("  API Key", current_key)
    base_url = _prompt_str("  Base URL", current_url or "https://api.openai.com/v1")
    model = _prompt_str("  Model", current_model or "gpt-4o")

    return {
        "LLM_API_KEY": api_key,
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": model,
    }


def _setup_preferences() -> dict[str, str]:
    """Configure common user-facing behaviour."""
    env = _read_env()

    print()
    print("─" * 48)
    print("  常用偏好")
    print("─" * 48)
    print("  联网工具用于 web_search / web_fetch；时区影响时间显示和定时任务。")

    web_enabled = _prompt_yn(
        "  启用原生联网工具?",
        default=_env_bool(env, "WEB_TOOL_ENABLED", True),
    )
    timezone = _prompt_timezone(env.get("CLAW_TIMEZONE", ""))
    return {
        "WEB_TOOL_ENABLED": "true" if web_enabled else "false",
        "CLAW_TIMEZONE": timezone,
    }


def _setup_gateway() -> dict[str, str]:
    """Configure local or LAN Gateway access safely."""
    env = _read_env()
    current_host = env.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    current_port_raw = env.get("GATEWAY_PORT", "8000").strip()
    try:
        current_port = int(current_port_raw)
    except ValueError:
        current_port = 8000
    if not 1 <= current_port <= 65535:
        current_port = 8000
    current_is_lan = current_host not in {"127.0.0.1", "::1", "localhost"}

    print()
    print("─" * 48)
    print("  Gateway 配置")
    print("─" * 48)
    print("  1. 仅本机访问（推荐，更安全）")
    print("  2. 局域网访问（需要访问令牌和受信任来源）")
    while True:
        mode = _prompt_str("  访问方式", "2" if current_is_lan else "1")
        if mode in {"1", "2"}:
            break
        print("  请输入 1 或 2。")

    port = _prompt_int("  监听端口", current_port, minimum=1, maximum=65535)
    open_browser = _prompt_yn(
        "  启动 Gateway 时自动打开浏览器?",
        default=_env_bool(env, "GATEWAY_OPEN_BROWSER", False),
    )
    updates = {
        "GATEWAY_HOST": "127.0.0.1" if mode == "1" else "0.0.0.0",
        "GATEWAY_PORT": str(port),
        "GATEWAY_OPEN_BROWSER": "true" if open_browser else "false",
    }
    if mode == "1":
        print("  已选择仅本机访问；现有远程令牌和来源配置将原样保留。")
        return updates

    current_token = env.get("GATEWAY_API_TOKEN", "").strip()
    if current_token:
        print(f"  当前访问令牌: {_mask_secret(current_token)}")
        if _prompt_yn("  重新生成访问令牌?", default=False):
            current_token = secrets.token_urlsafe(32)
            print("  已生成新的随机访问令牌。")
    else:
        current_token = secrets.token_urlsafe(32)
        print("  已自动生成随机访问令牌。")

    origins = _prompt_origins(env.get("GATEWAY_ALLOWED_ORIGINS", ""), port)
    updates.update({
        "GATEWAY_API_TOKEN": current_token,
        "GATEWAY_ALLOWED_ORIGINS": origins,
    })
    return updates


def _setup_advanced() -> dict[str, str]:
    """Configure the small set of advanced options useful during onboarding."""
    env = _read_env()

    def _current_int(name: str, default: int, minimum: int) -> int:
        try:
            value = int(env.get(name, str(default)))
        except ValueError:
            return default
        return value if value >= minimum else default

    def _current_float(name: str, default: float, minimum: float) -> float:
        try:
            value = float(env.get(name, str(default)))
        except ValueError:
            return default
        return value if math.isfinite(value) and value >= minimum else default

    print()
    print("─" * 48)
    print("  高级模型与搜索配置")
    print("─" * 48)
    print("  不确定时直接回车使用当前值或推荐默认值。")

    context_window = _prompt_int(
        "  模型上下文窗口（token）",
        _current_int("LLM_CONTEXT_WINDOW", 32000, 1024),
        minimum=1024,
    )
    max_output = _prompt_int(
        "  最大输出 token",
        _current_int("LLM_MAX_OUTPUT_TOKENS", 4096, 1),
        minimum=1,
    )
    retries = _prompt_int(
        "  API 最大重试次数",
        _current_int("LLM_MAX_RETRIES", 2, 0),
        minimum=0,
    )
    timeout = _prompt_float(
        "  单次 API 请求超时（秒）",
        _current_float("LLM_REQUEST_TIMEOUT", 120.0, 1.0),
        minimum=1.0,
    )

    current_tavily = env.get("TAVILY_API_KEY", "").strip()
    tavily_update: str | None = None
    if current_tavily:
        print(f"  当前 Tavily API Key: {_mask_secret(current_tavily)}")
        if _prompt_yn("  修改 Tavily API Key?", default=False):
            tavily_update = _prompt_secret("  Tavily API Key", current_tavily)
    elif _prompt_yn("  配置 Tavily API Key?（可跳过，联网搜索仍可使用）", default=False):
        tavily_update = _prompt_secret("  Tavily API Key")

    updates = {
        "LLM_CONTEXT_WINDOW": str(context_window),
        "LLM_MAX_OUTPUT_TOKENS": str(max_output),
        "LLM_MAX_RETRIES": str(retries),
        "LLM_REQUEST_TIMEOUT": f"{timeout:g}",
    }
    if tavily_update is not None:
        updates["TAVILY_API_KEY"] = tavily_update
    return updates


def _setup_qq() -> dict[str, str]:
    """Configure QQ Bot channel. Returns env updates."""
    env = _read_env()

    print()
    print("─" * 48)
    print("  QQ Bot 配置")
    print("─" * 48)

    current_app_id = env.get("QQ_APP_ID", "")
    current_secret = env.get("QQ_CLIENT_SECRET", "")
    current_allow = env.get("QQ_ALLOW_FROM", "")
    current_format = env.get("QQ_MSG_FORMAT", "markdown").strip().lower()
    if current_format == "plain":
        current_format = "text"
    if current_format not in {"markdown", "text"}:
        current_format = "markdown"
    current_ack = env.get("QQ_ACK_MESSAGE", "")

    enabled = _prompt_yn("  启用 QQ Bot?", default=True)
    if not enabled:
        print("  QQ Bot 将被禁用；现有凭证会保留，之后可再次启用。")
        return {"QQ_ENABLED": "false"}

    updates: dict[str, str] = {"QQ_ENABLED": "true"}
    configure_credentials = True
    if current_app_id and current_secret:
        print("  QQ 凭证已配置:")
        print(f"    QQ_APP_ID        = {current_app_id}")
        print("    QQ_CLIENT_SECRET = ****")
        print()
        configure_credentials = _prompt_yn("  是否重新配置凭证?", default=False)

    qr_allow = ""
    if configure_credentials:
        print()
        print("  方式 1: 扫码自动获取（需先在另一终端运行 sjtuclaw gateway）")
        print("  方式 2: 手动输入 AppID 和 AppSecret")
        while True:
            choice = _prompt_str("  选择方式", "1")
            if choice in {"1", "2"}:
                break
            print("  请输入 1 或 2。")

        if choice == "1":
            from claw.channels.qq_onboard import qr_register

            print()
            result = qr_register()
            if result is None:
                print("  扫码失败，请手动输入。")
                choice = "2"
            else:
                updates.update({
                    "QQ_APP_ID": result["app_id"],
                    "QQ_CLIENT_SECRET": result["client_secret"],
                })
                qr_allow = result.get("user_openid", "")

        if choice == "2":
            updates.update({
                "QQ_APP_ID": _prompt_str("  AppID", current_app_id),
                "QQ_CLIENT_SECRET": _prompt_secret("  AppSecret", current_secret),
            })

    allow = _prompt_str(
        "  允许的 QQ OpenID（逗号分隔；* = 所有人，留空 = 拒绝所有人）",
        current_allow or qr_allow,
    )
    while True:
        msg_format = _prompt_str("  消息格式（markdown/text）", current_format).lower()
        if msg_format in {"markdown", "text"}:
            break
        print("  消息格式只能是 markdown 或 text。")
    ack = _prompt_str("  收到消息时的确认回复（可留空）", current_ack)
    updates.update({
        "QQ_ALLOW_FROM": allow,
        "QQ_MSG_FORMAT": msg_format,
        "QQ_ACK_MESSAGE": ack,
    })
    return updates


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
    if not _prompt_yn("  是否配置 QQ Bot?", default=False):
        return updates

    updates.update(_setup_qq())

    if updates:
        print()
        print("  通道配置完成。")
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
    print("  欢迎使用 SJTUClaw，让我们一起完成首次配置！")

    all_updates: dict[str, str] = {}

    # Step 1: LLM
    if _prompt_yn("\n  是否配置 LLM?", default=True):
        all_updates.update(_setup_llm())

    # Step 2: Common preferences
    if _prompt_yn("\n  是否配置联网工具和时区?", default=True):
        all_updates.update(_setup_preferences())

    # Step 3: Gateway
    if _prompt_yn("\n  是否配置 Gateway 访问方式?", default=True):
        all_updates.update(_setup_gateway())

    # Step 4: Advanced LLM/search controls
    if _prompt_yn("\n  是否配置高级模型与搜索参数?", default=False):
        all_updates.update(_setup_advanced())

    # Step 5: Channels
    all_updates.update(_setup_channels())

    # Write
    if all_updates:
        _write_env(all_updates)
        print()
        print("=" * 56)
        print("  配置已保存到 .env")
        print()
        masked_keys = {
            "LLM_API_KEY",
            "QQ_CLIENT_SECRET",
            "GATEWAY_API_TOKEN",
            "TAVILY_API_KEY",
        }
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


def _cmd_tui() -> int:
    """Start the full-screen terminal interface."""
    from claw.tui import main as tui_main
    return tui_main()


def _cmd_desktop() -> int:
    """Start the local Gateway and desktop window."""
    from claw.desktop import main as desktop_main
    return desktop_main()


def main() -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="sjtuclaw",
        description="SJTUClaw — AI Agent with QQ Bot support",
    )
    sub = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True,
    )

    sub.add_parser("gateway", help="Start the HTTP + WebSocket gateway")
    sub.add_parser("chat",    help="Start interactive CLI chat")
    sub.add_parser("tui",     help="Start the full-screen terminal UI")
    sub.add_parser("setup",   help="Interactive setup wizard")
    sub.add_parser("desktop", help="Start the local Gateway and desktop window")

    args = parser.parse_args()

    if args.command == "gateway":
        return _cmd_gateway()
    elif args.command == "chat":
        return _cmd_chat()
    elif args.command == "tui":
        return _cmd_tui()
    elif args.command == "desktop":
        return _cmd_desktop()
    elif args.command == "setup":
        return _cmd_setup()
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
