"""Desktop launcher for the packaged SJTUClaw app."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
import traceback
from pathlib import Path

import uvicorn

from claw.paths import resource_root, user_root
from claw.utils import force_utf8_stdio


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _choose_port() -> int:
    requested = int(os.getenv("GATEWAY_PORT", "8000"))
    if _port_available(requested):
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(url: str, timeout_s: float = 20.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5):
                return
        except Exception:
                time.sleep(0.2)


def _log_path() -> Path:
    path = user_root() / "logs" / "desktop.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _log(message: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")
    except OSError:
        pass


def _run_server(host: str, port: int) -> None:
    try:
        config = uvicorn.Config(
            "claw.gateway.server:app",
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception:
        _log(traceback.format_exc())
        raise


def _window_icon_path() -> str | None:
    candidates = [
        Path(sys.executable).resolve().parent / "SJTUClaw.ico",
        resource_root() / "packaging" / "windows" / "assets" / "SJTUClaw.ico",
        resource_root() / "web" / "favicon.ico",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run_window(url: str) -> None:
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    webview.create_window(
        "SJTUClaw",
        url,
        width=1280,
        height=820,
        min_size=(960, 640),
        text_select=True,
    )
    webview.start(icon=_window_icon_path())


def main() -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="SJTUClaw desktop launcher")
    parser.add_argument("--pet", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--server-only", action="store_true", help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args()
    if args.pet:
        # Keep the Tk-based pet isolated from the main desktop launcher.
        # This also lets the launcher report packaging problems without
        # crashing before the Gateway or webview window can start.
        from claw.pet.__main__ import main as pet_main

        sys.argv = [sys.argv[0], *(arg for arg in sys.argv[1:] if arg != "--pet")]
        return pet_main()

    host = "127.0.0.1"
    port = _choose_port()
    os.environ["GATEWAY_HOST"] = host
    os.environ["GATEWAY_PORT"] = str(port)
    url = f"http://{host}:{port}"
    _log(f"Starting SJTUClaw desktop gateway at {url}")

    if args.server_only:
        _run_server(host, port)
        return 0

    thread = threading.Thread(target=_run_server, args=(host, port), daemon=True)
    thread.start()
    _wait_until_ready(url)
    _run_window(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
