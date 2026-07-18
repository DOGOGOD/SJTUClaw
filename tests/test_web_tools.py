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


def test_resolver_prefers_ipv4_and_rotates_validated_addresses(monkeypatch):
    from claw.tools import web

    answers = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers)
    first = web._resolve_public_target("https://example.com", 0)[1]
    second = web._resolve_public_target("https://example.com", 1)[1]
    third = web._resolve_public_target("https://example.com", 2)[1]
    assert first.startswith("https://93.184.216.34")
    assert second.startswith("https://93.184.216.35")
    assert third.startswith("https://[2606:2800:220:1::1]")


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


def test_fetch_accepts_vendor_json_content_type(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/problem+json"},
            json={"detail": "readable"},
            request=request,
        )

    result = _fetch(
        "https://example.com/problem",
        WebToolConfig(max_retries=0),
        max_chars=5000,
        client_factory=_mock_client(handler),
    )
    assert "readable" in result["content"]


def test_fetch_rotates_to_next_ip_after_network_failure(monkeypatch):
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answers)
    seen_hosts = []

    def handler(request):
        seen_hosts.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("edge unavailable", request=request)
        return httpx.Response(200, text="recovered", request=request)

    result = _fetch(
        "https://example.com",
        WebToolConfig(max_retries=1),
        max_chars=5000,
        client_factory=_mock_client(handler),
    )
    assert result["content"] == "recovered"
    assert seen_hosts == ["93.184.216.34", "93.184.216.35"]


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

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)
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

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        if "duckduckgo" in request.headers["host"]:
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
    assert payload["fallback_errors"] == ["duckduckgo: 未返回可用结果"]


def test_search_follows_validated_bing_region_redirect(monkeypatch):
    from claw.tools import web

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)
    seen_hosts = []

    def handler(request):
        seen_hosts.append(request.headers["host"])
        if request.headers["host"] == "www.bing.com":
            return httpx.Response(
                302,
                headers={"location": "https://cn.bing.com/search?q=redirect&format=rss"},
                request=request,
            )
        rss = """<rss><channel><item><title>Regional result</title>
        <link>https://example.com/region</link><description>Works</description>
        </item></channel></rss>"""
        return httpx.Response(200, text=rss, request=request)

    monkeypatch.setattr(web, "_client", _mock_client(handler))
    results = web._search_bing("redirect", 2, WebToolConfig(max_retries=0))
    assert results[0]["title"] == "Regional result"
    assert seen_hosts == ["www.bing.com", "cn.bing.com"]


def test_search_uses_duckduckgo_lite_when_html_is_empty(monkeypatch):
    from claw.tools import web

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        if request.headers["host"] == "html.duckduckgo.com":
            return httpx.Response(200, text="<html>challenge</html>", request=request)
        lite = """
        <a class="result-link" href="https://example.com/lite">Lite result</a>
        <td class="result-snippet">Lite snippet</td>
        """
        return httpx.Response(200, text=lite, request=request)

    monkeypatch.setattr(web, "_client", _mock_client(handler))
    tool = create_web_search_tool(WebToolConfig(max_retries=0))
    result = tool.handler({"query": "fallback", "max_results": 2})
    assert result.ok
    payload = json.loads(result.content)
    assert payload["provider"] == "duckduckgo"
    assert payload["results"][0]["title"] == "Lite result"


def test_search_reports_error_when_all_providers_return_empty(monkeypatch):
    from claw.tools import web

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC_DNS)

    def handler(request):
        if "bing.com" in request.headers["host"]:
            return httpx.Response(200, text="<rss><channel /></rss>", request=request)
        return httpx.Response(200, text="<html>empty</html>", request=request)

    monkeypatch.setattr(web, "_client", _mock_client(handler))
    tool = create_web_search_tool(WebToolConfig(max_retries=0))
    result = tool.handler({"query": "nothing", "max_results": 2})
    assert not result.ok
    assert "duckduckgo: 未返回可用结果" in result.error
    assert "bing: 未返回可用结果" in result.error


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
