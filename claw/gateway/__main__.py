"""Gateway entry point.

Usage:
    python -m claw.gateway

The server listens on the address/port specified by the environment
variables ``GATEWAY_HOST`` (default 127.0.0.1) and ``GATEWAY_PORT``
(default 8000).

Set ``GATEWAY_OPEN_BROWSER=1`` to automatically open the web UI on startup.
"""

from __future__ import annotations

import os
import sys
import webbrowser

import uvicorn

from claw.config import PROJECT_ROOT
from claw.utils import force_utf8_stdio


def _webui_exists() -> bool:
    """Check whether the built web UI is present."""
    web_dir = PROJECT_ROOT / "web"
    return (web_dir / "index.html").exists() and (web_dir / "assets").is_dir()


def main() -> int:
    force_utf8_stdio()
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    port_str = os.getenv("GATEWAY_PORT", "8000")
    try:
        port = int(port_str)
    except ValueError:
        print(f"GATEWAY_PORT 值无效: {port_str}", file=sys.stderr)
        return 1

    url = f"http://{host}:{port}"

    print("=" * 56)
    print("  🐾  SJTUClaw Gateway")
    print("=" * 56)
    print(f"  地址: {url}")
    if _webui_exists():
        print(f"  Web UI: {url}")
    else:
        print(f"  Web UI: (未构建 — 运行 cd webui && npm run build)")
    print(f"  API:    {url}/sessions")
    print("=" * 56)
    print("  Press Ctrl+C to stop.")
    print()

    # Optionally open the browser
    if os.getenv("GATEWAY_OPEN_BROWSER", "").strip() in ("1", "true", "yes"):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run("claw.gateway.server:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
