"""Safe, dependency-light web search and page fetching tools.

The tools in this module are read-only, but network access still crosses a
security boundary.  Every requested URL (including redirects) is validated
against local/private address ranges before a connection is attempted.
Responses are bounded, time-limited and retried only for transient failures.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urldefrag, unquote, urljoin, urlsplit, urlunsplit

import httpx

from claw.tools.base import Tool, ToolResult

_USER_AGENT = "SJTUClaw/0.1 (+https://github.com/SJTUClaw)"
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class WebToolConfig:
    """Runtime limits for web tools."""

    enabled: bool = True
    timeout_seconds: float = 15.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_retries: int = 2
    tavily_api_key: str = ""
    trust_env_proxy: bool = False

    @classmethod
    def from_env(cls) -> "WebToolConfig":
        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name, "").strip().lower()
            if not raw:
                return default
            return raw in {"1", "true", "yes", "on"}

        def _float(name: str, default: float, low: float, high: float) -> float:
            try:
                return min(high, max(low, float(os.getenv(name, "") or default)))
            except (TypeError, ValueError):
                return default

        def _int(name: str, default: int, low: int, high: int) -> int:
            try:
                return min(high, max(low, int(os.getenv(name, "") or default)))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=_bool("WEB_TOOL_ENABLED", True),
            timeout_seconds=_float("WEB_TIMEOUT_SECONDS", 15.0, 1.0, 60.0),
            max_response_bytes=_int(
                "WEB_MAX_RESPONSE_BYTES", 2 * 1024 * 1024, 64 * 1024, 10 * 1024 * 1024
            ),
            max_retries=_int("WEB_MAX_RETRIES", 2, 0, 4),
            tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
            trust_env_proxy=_bool("WEB_TRUST_ENV_PROXY", False),
        )


class WebSecurityError(ValueError):
    """Raised when a URL violates the public-network boundary."""


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return not any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _is_proxy_fake_ip(address: str) -> bool:
    """Return whether an address is from Clash/sing-box's common fake-IP pool.

    RFC 2544 reserves 198.18.0.0/15 for benchmarking.  Transparent proxy
    clients commonly synthesize external DNS answers from this range.  It is
    accepted only for resolved hostnames; literal URLs using the range remain
    blocked below.
    """
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]) in _PROXY_FAKE_IP_NETWORK
    except ValueError:
        return False


def _resolve_public_target(url: str) -> tuple[str, str, str, str]:
    """Return normalized URL plus an IP-pinned connection target.

    The HTTP client must not resolve the hostname again after validation;
    otherwise a DNS-rebinding attacker can swap a public answer for a private
    one between the check and the connection.
    """
    """Validate and normalize a public HTTP(S) URL.

    Hostnames are resolved up front and all returned addresses must be public.
    Redirect targets are validated separately by the caller.
    """
    if not isinstance(url, str) or not url.strip():
        raise WebSecurityError("URL 不能为空")
    if len(url) > 4096:
        raise WebSecurityError("URL 过长")

    normalized = url.strip()
    parts = urlsplit(normalized)
    if parts.scheme.lower() not in {"http", "https"}:
        raise WebSecurityError("仅允许访问 http 或 https URL")
    if not parts.hostname:
        raise WebSecurityError("URL 缺少有效主机名")
    if parts.username is not None or parts.password is not None:
        raise WebSecurityError("URL 不允许包含用户名或密码")

    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise WebSecurityError("internal/private URL detected: 禁止访问本地或内部主机")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise WebSecurityError(f"URL 端口无效：{exc}") from exc

    # Literal IPs do not need DNS.  For hostnames, reject the destination if
    # any answer is non-public; choosing one safe answer from a mixed set can
    # otherwise enable DNS rebinding/failover attacks.
    selected_address: str
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise WebSecurityError(f"无法解析主机名 {host}：{exc}") from exc
        addresses = {item[4][0] for item in answers if item[4]}
        if not addresses or not all(
            _is_public_ip(address) or _is_proxy_fake_ip(address) for address in addresses
        ):
            raise WebSecurityError("internal/private URL detected: 主机解析到非公网地址")
        selected_address = sorted(addresses)[0]
    else:
        if not _is_public_ip(str(literal)):
            raise WebSecurityError("internal/private URL detected: 禁止访问非公网地址")
        selected_address = str(literal)

    default_port = 443 if parts.scheme.lower() == "https" else 80
    explicit_port = parts.port
    ip_literal = f"[{selected_address}]" if ":" in selected_address else selected_address
    netloc = ip_literal if (explicit_port is None or explicit_port == default_port) else f"{ip_literal}:{explicit_port}"
    connect_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    host_header = host if (explicit_port is None or explicit_port == default_port) else f"{host}:{explicit_port}"
    return normalized, connect_url, host_header, host


def validate_public_url(url: str) -> str:
    """Validate and normalize a public HTTP(S) URL."""
    return _resolve_public_target(url)[0]


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def _search_result_url_allowed(url: str) -> bool:
    """Cheaply reject obviously unsafe URLs without doing DNS for search pages."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True
    return _is_public_ip(str(literal))


def _dedupe_links(links: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for link in links:
        url = str(link.get("url", "")).strip()
        text = " ".join(str(link.get("text", "")).split())
        if not _search_result_url_allowed(url):
            continue
        key = urldefrag(url)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"text": text[:300], "url": url[:4096]})
    return deduped


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base_url: str, *, max_links: int = 40) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_links = max_links
        self.skip_depth = 0
        self.title_depth = 0
        self.title: list[str] = []
        self.description = ""
        self.canonical_url = ""
        self.robots = ""
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._current_link: dict[str, str] | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {key.lower(): value for key, value in attrs if key}
        if tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            content = " ".join((attr.get("content") or "").split())
            if content and name in {"description", "og:description"} and not self.description:
                self.description = content
            elif content and name == "robots":
                self.robots = content
        elif tag == "link":
            rels = set((attr.get("rel") or "").lower().split())
            href = attr.get("href") or ""
            if "canonical" in rels and href and not self.canonical_url:
                self.canonical_url = urljoin(self.base_url, href)
        elif tag == "a" and len(self.links) < self.max_links:
            href = attr.get("href") or ""
            absolute = urljoin(self.base_url, href)
            if urlsplit(absolute).scheme.lower() in {"http", "https"}:
                self._current_link = {"url": absolute[:4096], "text": ""}
                self._link_text = []
        if tag in self._SKIP:
            self.skip_depth += 1
        if tag == "title":
            self.title_depth += 1
        if tag in {"article", "section", "p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag == "a" and self._current_link is not None:
            self._current_link["text"] = " ".join("".join(self._link_text).split())[:300]
            if self._current_link["text"] or self._current_link["url"]:
                self.links.append(self._current_link)
            self._current_link = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.title_depth:
            self.title.append(data)
        if self._current_link is not None:
            self._link_text.append(data)
        self.parts.append(data)

    def result(self) -> dict[str, Any]:
        title = " ".join(" ".join(self.title).split())
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return {
            "title": title,
            "description": self.description,
            "canonical_url": self.canonical_url,
            "robots": self.robots,
            "content": "\n".join(line for line in lines if line),
            "links": _dedupe_links(self.links),
        }


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._finish_current()
            href = attr.get("href") or ""
            query = parse_qs(urlsplit(href).query)
            if "uddg" in query:
                href = unquote(query["uddg"][0])
            self._current = {"title": "", "url": href, "snippet": ""}
            self._capture = "title"
            self._text = []
        elif self._current is not None and "result__snippet" in classes:
            self._capture = "snippet"
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "a" and self._capture == "title":
            self._current["title"] = " ".join("".join(self._text).split())
            self._capture = ""
        elif tag in {"a", "div"} and self._capture == "snippet":
            self._current["snippet"] = " ".join("".join(self._text).split())
            self._finish_current()
            self._capture = ""

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def close(self) -> None:
        self._finish_current()
        super().close()

    def _finish_current(self) -> None:
        if self._current is not None and self._current["title"] and self._current["url"]:
            self.results.append(self._current)
        self._current = None


def _decode_body(body: bytes, content_type: str, encoding: str | None = None) -> str:
    charset = encoding
    if not charset and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _client(config: WebToolConfig) -> httpx.Client:
    timeout = httpx.Timeout(config.timeout_seconds, connect=min(10.0, config.timeout_seconds))
    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=config.trust_env_proxy,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*;q=0.5"},
    )


def _read_limited(chunks: Iterable[bytes], limit: int) -> tuple[bytes, bool]:
    data = bytearray()
    truncated = False
    for chunk in chunks:
        remaining = limit - len(data)
        if remaining <= 0:
            truncated = True
            break
        data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
            break
    return bytes(data), truncated


def _fetch(
    url: str,
    config: WebToolConfig,
    *,
    max_chars: int,
    client_factory: Callable[[WebToolConfig], httpx.Client] = _client,
) -> dict[str, Any]:
    current = url.strip()
    attempts = 0
    redirects = 0
    while True:
        try:
            current, connect_url, host_header, sni_hostname = _resolve_public_target(current)
            with client_factory(config) as client:
                with client.stream(
                    "GET",
                    connect_url,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": sni_hostname},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location:
                            raise RuntimeError("服务器返回重定向但未提供 Location")
                        redirects += 1
                        if redirects > 5:
                            raise RuntimeError("重定向次数超过上限（5）")
                        current = urljoin(current, location)
                        continue
                    if response.status_code in _TRANSIENT_STATUS and attempts < config.max_retries:
                        attempts += 1
                        time.sleep(min(0.25 * (2 ** (attempts - 1)), 1.0))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and not any(content_type.startswith(t) for t in _TEXT_CONTENT_TYPES):
                        raise RuntimeError(f"不支持的响应类型：{content_type.split(';', 1)[0]}")
                    body, byte_truncated = _read_limited(response.iter_bytes(), config.max_response_bytes)
                    text = _decode_body(body, content_type, response.encoding)
                    title = ""
                    description = ""
                    canonical_url = ""
                    robots = ""
                    links: list[dict[str, str]] = []
                    if "html" in content_type or "<html" in text[:500].lower():
                        parser = _TextExtractor(current)
                        parser.feed(text)
                        parsed = parser.result()
                        title = parsed["title"]
                        description = parsed["description"]
                        canonical_url = parsed["canonical_url"]
                        robots = parsed["robots"]
                        text = parsed["content"]
                        links = parsed["links"]
                    text = html.unescape(text)
                    char_truncated = len(text) > max_chars
                    if char_truncated:
                        text = text[:max_chars]
                    return {
                        "url": url,
                        "final_url": current,
                        "status_code": response.status_code,
                        "content_type": content_type.split(";", 1)[0] or "unknown",
                        "title": title,
                        "description": html.unescape(description),
                        "canonical_url": canonical_url,
                        "robots": robots,
                        "content": text,
                        "links": links,
                        "truncated": byte_truncated or char_truncated,
                    }
        except WebSecurityError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempts >= config.max_retries:
                raise RuntimeError(f"网络请求失败（已重试 {attempts} 次）：{exc}") from exc
            attempts += 1
            time.sleep(min(0.25 * (2 ** (attempts - 1)), 1.0))


def _request_text(
    method: str,
    url: str,
    config: WebToolConfig,
    *,
    json_data: dict[str, Any] | None = None,
    form_data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Request text with the same retry and size guarantees as web_fetch."""
    attempts = 0
    while True:
        try:
            with _client(config) as client:
                with client.stream(
                    method, url, json=json_data, data=form_data, params=params
                ) as response:
                    if response.status_code in _TRANSIENT_STATUS and attempts < config.max_retries:
                        attempts += 1
                        time.sleep(min(0.25 * (2 ** (attempts - 1)), 1.0))
                        continue
                    response.raise_for_status()
                    body, truncated = _read_limited(response.iter_bytes(), config.max_response_bytes)
                    if truncated:
                        raise RuntimeError("搜索服务响应超过大小上限")
                    return _decode_body(
                        body,
                        response.headers.get("content-type", ""),
                        response.encoding,
                    )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempts >= config.max_retries:
                raise RuntimeError(f"搜索请求失败（已重试 {attempts} 次）：{exc}") from exc
            attempts += 1
            time.sleep(min(0.25 * (2 ** (attempts - 1)), 1.0))


def _safe_search_results(results: Iterable[dict[str, str]], max_results: int) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in results:
        url = str(item.get("url", "")).strip()
        if not _search_result_url_allowed(url):
            continue
        key = urldefrag(url)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        safe.append(
            {
                "title": " ".join(str(item.get("title", "")).split())[:500],
                "url": url[:4096],
                "snippet": " ".join(str(item.get("snippet", "")).split())[:2000],
            }
        )
        if len(safe) >= max_results:
            break
    return safe


def _search_tavily(query: str, max_results: int, config: WebToolConfig) -> list[dict[str, str]]:
    text = _request_text(
        "POST",
        "https://api.tavily.com/search",
        config,
        json_data={
            "api_key": config.tavily_api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        },
    )
    payload = json.loads(text)
    candidates = [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        }
        for item in payload.get("results", [])[:max_results]
        if isinstance(item, dict) and item.get("url")
    ]
    return _safe_search_results(candidates, max_results)


def _search_duckduckgo(query: str, max_results: int, config: WebToolConfig) -> list[dict[str, str]]:
    text = _request_text(
        "POST",
        "https://html.duckduckgo.com/html/",
        config,
        form_data={"q": query},
    )
    parser = _DuckDuckGoParser()
    parser.feed(text)
    parser.close()
    return _safe_search_results(parser.results, max_results)


def _search_bing(query: str, max_results: int, config: WebToolConfig) -> list[dict[str, str]]:
    text = _request_text(
        "GET",
        "https://www.bing.com/search",
        config,
        params={"q": query, "format": "rss"},
    )
    root = ET.fromstring(text)
    candidates: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        candidates.append(
            {
                "title": item.findtext("title", default=""),
                "url": item.findtext("link", default=""),
                "snippet": item.findtext("description", default=""),
            }
        )
    return _safe_search_results(candidates, max_results)


def create_web_fetch_tool(config: WebToolConfig | None = None) -> Tool:
    cfg = config or WebToolConfig.from_env()

    def handler(args: dict[str, Any]) -> ToolResult:
        try:
            max_chars = _clamp_int(args.get("max_chars"), 30000, 1000, 100000)
            result = _fetch(args["url"], cfg, max_chars=max_chars)
            return ToolResult(ok=True, content=json.dumps(result, ensure_ascii=False))
        except WebSecurityError as exc:
            return ToolResult(ok=False, error=str(exc))
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            return ToolResult(ok=False, error=f"web_fetch 失败：{exc}")

    return Tool(
        name="web_fetch",
        description=(
            "抓取公开 HTTP(S) 网页并提取可读文本。会阻止 localhost、内网地址和危险重定向，"
            "并限制响应大小。适合读取已知 URL；搜索未知资料请使用 web_search。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 4096, "description": "公开网页 URL"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000, "description": "返回正文的最大字符数"},
            },
            "required": ["url"],
        },
        handler=handler,
        safety_level="network",
        concurrency_safe=True,
        max_result_chars=120000,
    )


def create_web_search_tool(config: WebToolConfig | None = None) -> Tool:
    cfg = config or WebToolConfig.from_env()

    def handler(args: dict[str, Any]) -> ToolResult:
        query = args["query"].strip()
        max_results = _clamp_int(args.get("max_results"), 5, 1, 10)
        if not query:
            return ToolResult(ok=False, error="web_search 失败：query 不能只包含空白字符")
        providers: list[tuple[str, Callable]] = []
        if cfg.tavily_api_key:
            providers.append(("tavily", _search_tavily))
        providers.extend((("duckduckgo", _search_duckduckgo), ("bing", _search_bing)))
        failures: list[str] = []
        provider = providers[-1][0]
        results: list[dict[str, str]] = []
        for provider, search_fn in providers:
            try:
                results = search_fn(query, max_results, cfg)
            except (httpx.HTTPError, RuntimeError, ValueError, ET.ParseError) as exc:
                failures.append(f"{provider}: {exc}")
                continue
            if results:
                break
        if not results and failures:
            return ToolResult(
                ok=False,
                error="web_search 所有搜索后端均失败：" + "；".join(failures),
            )
        return ToolResult(
            ok=True,
            content=json.dumps(
                {"query": query, "provider": provider, "result_count": len(results), "results": results},
                ensure_ascii=False,
            ),
        )

    return Tool(
        name="web_search",
        description=(
            "搜索互联网并返回标题、URL 和摘要。配置 TAVILY_API_KEY 时使用 Tavily，"
            "否则使用 DuckDuckGo，并在无结果或故障时回退到 Bing。"
            "需要读取结果全文时再调用 web_fetch。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "搜索关键词或问题"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "结果数量，默认 5"},
            },
            "required": ["query"],
        },
        handler=handler,
        safety_level="network",
        concurrency_safe=True,
        max_result_chars=30000,
    )


def register_web_tools(registry, config: WebToolConfig | None = None) -> None:
    cfg = config or WebToolConfig.from_env()
    if not cfg.enabled:
        return
    registry.register(create_web_search_tool(cfg))
    registry.register(create_web_fetch_tool(cfg))
