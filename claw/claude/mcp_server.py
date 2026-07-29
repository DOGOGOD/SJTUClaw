"""Minimal stdio MCP server exposing SJTUClaw host tools to Claude Code."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any


_MAX_MESSAGE_CHARS = 8 * 1024 * 1024
_BRIDGE_TIMEOUT_S = 310.0


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("tool manifest is too large")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid tool manifest")
    tools = payload.get("tools")
    if payload.get("version") != 1 or not isinstance(tools, list):
        raise ValueError("invalid tool manifest")
    return [
        tool
        for tool in tools
        if (
            isinstance(tool, dict)
            and isinstance(tool.get("name"), str)
            and isinstance(tool.get("description"), str)
            and isinstance(tool.get("parameters"), dict)
        )
    ]


class HostToolMcpServer:
    def __init__(
        self,
        tools: list[dict[str, Any]],
        relay_dir: Path,
        token: str,
    ):
        self._tools = {tool["name"]: tool for tool in tools}
        self._relay_dir = relay_dir
        self._token = token

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if request_id is None:
            return None
        try:
            if method == "initialize":
                requested = str(params.get("protocolVersion") or "2025-06-18")
                result = {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "SJTUClaw Host Tools",
                        "version": "1.0.0",
                    },
                    "instructions": (
                        "These are SJTUClaw capabilities that complement Claude Code. "
                        "Use recall for stored user preferences, projects, and decisions; "
                        "use remember for durable new facts; use cron for persistent "
                        "SJTUClaw reminders and scheduled tasks."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [self._mcp_tool(tool) for tool in self._tools.values()]}
            elif method == "tools/call":
                result = self._call_tool(params)
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return self._error(request_id, -32000, str(exc))

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message[:1000]},
        }

    @staticmethod
    def _mcp_tool(tool: dict[str, Any]) -> dict[str, Any]:
        safety = str(tool.get("safety_level") or "")
        # cron contains both list (read-only) and add/remove (mutating).
        read_only = (
            safety in {"read_only", "network"}
            and tool.get("name") != "cron"
        )
        return {
            "name": tool["name"],
            "title": f"SJTUClaw: {tool['name']}",
            "description": tool["description"],
            "inputSchema": tool["parameters"],
            "annotations": {
                "readOnlyHint": read_only,
                "destructiveHint": not read_only,
                "idempotentHint": read_only,
                "openWorldHint": safety == "network",
            },
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = (
            params.get("arguments")
            if isinstance(params.get("arguments"), dict)
            else {}
        )
        if name not in self._tools:
            return {
                "content": [{"type": "text", "text": f"未知的 SJTUClaw tool: {name}"}],
                "isError": True,
            }
        response = self._exchange(
            {
                "kind": "host_tool",
                "token": self._token,
                "toolName": name,
                "input": arguments,
            }
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": str(response.get("result") or "(空结果)"),
                }
            ],
            "isError": response.get("ok") is not True,
        }

    def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        pending = self._relay_dir / f"pending.{request_id}"
        request = self._relay_dir / f"request.{request_id}.json"
        response = self._relay_dir / f"request.{request_id}.json.response"
        try:
            pending.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            pending.replace(request)
            deadline = time.monotonic() + _BRIDGE_TIMEOUT_S
            while time.monotonic() < deadline:
                if response.is_file():
                    result = json.loads(response.read_text(encoding="utf-8"))
                    if isinstance(result, dict):
                        return result
                    raise ValueError("invalid host tool response")
                time.sleep(0.05)
            raise TimeoutError("SJTUClaw host tool bridge timed out")
        finally:
            pending.unlink(missing_ok=True)
            request.unlink(missing_ok=True)
            response.unlink(missing_ok=True)


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def serve(manifest: Path, relay_dir: Path, token: str) -> int:
    tools = _load_manifest(manifest)
    server = HostToolMcpServer(tools, relay_dir, token)
    while True:
        line = sys.stdin.readline(_MAX_MESSAGE_CHARS + 1)
        if not line:
            break
        if len(line) > _MAX_MESSAGE_CHARS:
            while line and not line.endswith("\n"):
                line = sys.stdin.readline(_MAX_MESSAGE_CHARS + 1)
            _write_message(
                HostToolMcpServer._error(None, -32600, "MCP message is too large")
            )
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
        except (json.JSONDecodeError, ValueError) as exc:
            _write_message(HostToolMcpServer._error(None, -32700, str(exc)))
            continue
        response = server.handle(message)
        if response is not None:
            _write_message(response)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--relay-dir", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    relay_dir = Path(args.relay_dir).resolve()
    if not relay_dir.is_dir():
        return 2
    return serve(Path(args.manifest).resolve(), relay_dir, args.token)


if __name__ == "__main__":
    raise SystemExit(main())
