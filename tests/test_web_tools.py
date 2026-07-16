"""Tests for native web tools and hardened tool dispatch."""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from claw.tools.base import Tool, ToolRegistry, ToolRegistryError, ToolResult
from claw.tools.web import (
    WebSecurityError,
    WebToolConfig,
    _fetch,
    create_web_search_tool,
    validate_public_url,
)


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _mock_client(handler):
    transport = httpx.MockTransport(handler)

    def factory(config):
        return httpx.Client(transport=transport, trust_env=False)

    return factory


def test_validate_public_url_blocks_local_and_credentials():
    for url in (
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://localhost/",
        "http://user:pass@example.com/",
        "file:///etc/passwd",
    ):
        with pytest.raises(WebSecurityError):
            validate_public_url(url)


def test_validate_public_url_rejects_mixed_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: PUBLIC_DNS
        + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
    )
    with pytest.raises(WebSecurityError, match="非公网"):
        validate_public_url("https://example.com")


def test_validate_public_url_supports_proxy_fake_dns_but_blocks_literal(monkeypatch):
    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake_dns)
    assert validate_public_url("https://example.com") == "https://example.com"
    with pytest.raises(WebSecurityError):
        validate_public_url("https://198.18.0.42/")


def test_fetch_extracts_html_and_decodes_utf8(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content="<html><title>测试标题</title><body><script>bad()</script><h1>你好，世界</h1></body></html>".encode(),
            request=request,
        )

    result = _fetch(
        "https://example.com/page",
        WebToolConfig(max_retries=0),
        max_chars=5000,
        client_factory=_mock_client(handler),
    )
    assert result["title"] == "测试标题"
    assert "你好，世界" in result["content"]
    assert "bad()" not in result["content"]
    assert not result["truncated"]


def test_fetch_returns_html_metadata_and_links(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    html = """
    <html>
      <head>
        <title>Doc title</title>
        <meta name="description" content="Short page summary">
        <meta name="robots" content="noindex">
        <link rel="canonical" href="/canonical">
      </head>
      <body>
        <a href="/docs">Docs</a>
        <a href="http://127.0.0.1/private">Private</a>
        <a href="/docs#section">Duplicate docs</a>
      </body>
    </html>
    """

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=html,
            request=request,
        )

    result = _fetch(
        "https://example.com/page",
        WebToolConfig(max_retries=0),
        max_chars=5000,
        client_factory=_mock_client(handler),
    )
    assert result["description"] == "Short page summary"
    assert result["canonical_url"] == "https://example.com/canonical"
    assert result["robots"] == "noindex"
    assert result["links"] == [{"text": "Docs", "url": "https://example.com/docs"}]


def test_fetch_revalidates_redirect_target(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    with pytest.raises(WebSecurityError, match="private"):
        _fetch(
            "https://example.com",
            WebToolConfig(max_retries=0),
            max_chars=5000,
            client_factory=_mock_client(handler),
        )


def test_fetch_bounds_response_size(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * 100_000,
            request=request,
        )

    result = _fetch(
        "https://example.com",
        WebToolConfig(max_response_bytes=4096, max_retries=0),
        max_chars=2000,
        client_factory=_mock_client(handler),
    )
    assert result["truncated"]
    assert len(result["content"]) == 2000


def test_search_uses_duckduckgo_without_key(monkeypatch):
    from claw.tools import web

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)
    page = """
    <div class="result">
      <a class="result__a" href="https://example.com/a">Example result</a>
      <a class="result__snippet">Useful snippet</a>
    </div>
    """

    def handler(request):
        return httpx.Response(200, text=page, request=request)

    monkeypatch.setattr(web, "_client", _mock_client(handler))
    tool = create_web_search_tool(WebToolConfig(tavily_api_key="", max_retries=0))
    result = tool.handler({"query": "sjtu claw", "max_results": 3})
    assert result.ok
    payload = json.loads(result.content)
    assert payload["provider"] == "duckduckgo"
    assert payload["results"][0]["title"] == "Example result"


def test_search_filters_unsafe_and_duplicate_results(monkeypatch):
    from claw.tools import web

    page = """
    <div class="result">
      <a class="result__a" href="https://example.com/a#top">Example A</a>
      <a class="result__snippet">First snippet</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.com/a">Duplicate A</a>
      <a class="result__snippet">Duplicate snippet</a>
    </div>
    <div class="result">
      <a class="result__a" href="http://127.0.0.1/admin">Local result</a>
      <a class="result__snippet">Unsafe snippet</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.com/no-snippet">No snippet result</a>
    </div>
    """

    def handler(request):
        return httpx.Response(200, text=page, request=request)

    monkeypatch.setattr(web, "_client", _mock_client(handler))
    tool = create_web_search_tool(WebToolConfig(tavily_api_key="", max_retries=0))
    result = tool.handler({"query": "sjtu claw", "max_results": 10})
    assert result.ok
    payload = json.loads(result.content)
    assert [item["url"] for item in payload["results"]] == [
        "https://example.com/a#top",
        "https://example.com/no-snippet",
    ]


def test_search_falls_back_to_bing_when_duckduckgo_is_empty(monkeypatch):
    from claw.tools import web

    def handler(request):
        if "duckduckgo" in str(request.url):
            return httpx.Response(200, text="<html><body>no results</body></html>", request=request)
        rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss><channel><item><title>Bing result</title>
        <link>https://example.com/b</link><description>Fallback snippet</description>
        </item></channel></rss>"""
        return httpx.Response(200, text=rss, request=request)

    monkeypatch.setattr(web, "_client", _mock_client(handler))
    tool = create_web_search_tool(WebToolConfig(tavily_api_key="", max_retries=0))
    result = tool.handler({"query": "fallback", "max_results": 2})
    assert result.ok
    payload = json.loads(result.content)
    assert payload["provider"] == "bing"
    assert payload["results"][0]["title"] == "Bing result"


def test_register_all_tools_includes_native_web(monkeypatch):
    monkeypatch.setenv("WEB_TOOL_ENABLED", "true")
    registry = ToolRegistry()
    from claw.tools import register_all_tools

    register_all_tools(registry)
    assert "web_search" in registry.tool_names
    assert "web_fetch" in registry.tool_names
    assert registry.get_tool("web_fetch").safety_level == "network"


def test_registry_rejects_bad_tool_definition_and_result():
    registry = ToolRegistry()
    with pytest.raises(ToolRegistryError):
        registry.register(
            Tool(
                name="bad name",
                description="bad",
                input_schema={"type": "object", "properties": {}},
                handler=lambda args: ToolResult(ok=True, content="ok"),
            )
        )

    registry.register(
        Tool(
            name="bad_result",
            description="bad return type",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: "not a ToolResult",
        )
    )
    result = registry.execute_by_name("bad_result", {})
    assert not result.ok
    assert "返回格式无效" in result.error


def test_prepare_hook_fails_closed():
    registry = ToolRegistry()
    executed = False

    def handler(args):
        nonlocal executed
        executed = True
        return ToolResult(ok=True, content="unsafe")

    registry.register(
        Tool(
            name="guarded",
            description="guarded",
            input_schema={"type": "object", "properties": {}},
            handler=handler,
        )
    )
    registry.set_prepare_call(lambda name, args: (_ for _ in ()).throw(RuntimeError("boom")))
    result = registry.execute_by_name("guarded", {})
    assert not result.ok
    assert not executed
