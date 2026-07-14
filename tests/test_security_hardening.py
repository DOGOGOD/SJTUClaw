"""Regression tests for externally reachable security boundaries."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_gateway_rejects_untrusted_browser_origin():
    from claw.gateway.middleware import GatewaySecurityMiddleware

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware)

    @app.post("/command")
    def command():
        return {"ok": True}

    response = TestClient(app).post(
        "/command", headers={"Origin": "https://attacker.example"}
    )
    assert response.status_code == 403


def test_remote_gateway_client_requires_configured_token(monkeypatch):
    from claw.gateway.middleware import GatewaySecurityMiddleware

    monkeypatch.setenv("GATEWAY_API_TOKEN", "test-secret-token")
    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware)

    @app.get("/sessions")
    def sessions():
        return {"ok": True}

    client = TestClient(app, client=("203.0.113.5", 50000))
    assert client.get("/sessions").status_code == 401
    assert client.get(
        "/sessions", headers={"X-SJTUClaw-Token": "test-secret-token"}
    ).status_code == 200


def test_active_turn_registration_is_exclusive():
    from claw.gateway import server

    server._active_turns.clear()
    first = server._register_active_turn("same-session")
    assert first is not None
    assert server._register_active_turn("same-session") is None
    server._unregister_active_turn("same-session", first)
    assert server._register_active_turn("same-session") is not None
    server._active_turns.clear()


def test_qq_group_enforces_member_allowlist(monkeypatch):
    from claw.channels.qq import QQChannel, QQConfig

    channel = QQChannel(QQConfig(allow_from=["owner"]))
    handled = False

    async def handler(*args, **kwargs):
        nonlocal handled
        handled = True
        return "unsafe"

    channel.set_message_handler(handler)
    asyncio.run(channel._handle_group(
        {"group_openid": "group"}, "msg", "/auto on", {"member_openid": "intruder"}
    ))
    assert handled is False


def test_web_fetch_connects_to_the_validated_ip(monkeypatch):
    from claw.tools.web import WebToolConfig, _fetch

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        return httpx.Response(200, text="ok", request=request)

    def client_factory(config):
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    result = _fetch(
        "https://example.com/page",
        WebToolConfig(max_retries=0),
        max_chars=1000,
        client_factory=client_factory,
    )
    assert result["final_url"] == "https://example.com/page"


def test_upload_helper_removes_oversized_partial_file(tmp_path):
    from io import BytesIO
    from starlette.datastructures import UploadFile
    from claw.gateway.uploads import UploadTooLargeError, save_upload_limited

    upload = UploadFile(BytesIO(b"x" * 32), filename="large.bin")
    target = tmp_path / "large.bin"
    with pytest.raises(UploadTooLargeError):
        asyncio.run(save_upload_limited(upload, target, max_bytes=16, chunk_bytes=8))
    assert not target.exists()
