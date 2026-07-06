"""Gateway entry point.

Usage:
    python -m claw.gateway

The server listens on the address/port specified by the environment
variables ``GATEWAY_HOST`` (default 127.0.0.1) and ``GATEWAY_PORT``
(default 8000).
"""

from __future__ import annotations

import os
import sys

import uvicorn


def main() -> int:
    host = os.getenv("GATEWAY_HOST", "127.0.0.1")
    port_str = os.getenv("GATEWAY_PORT", "8000")
    try:
        port = int(port_str)
    except ValueError:
        print(f"GATEWAY_PORT 值无效: {port_str}", file=sys.stderr)
        return 1

    print(f"Starting SJTUClaw Gateway on http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    uvicorn.run("claw.gateway.server:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
