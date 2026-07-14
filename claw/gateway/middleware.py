"""Gateway middleware: rate limiting, request logging, request size validation.

Gateway middleware providing:
- Request size cap (MAX_REQUEST_BYTES)
- Per-client rate limiting
- Structured request logging with request IDs
"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# -- Configurable limits ------------------------------------------------------
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # 10 MB default
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB attachment cap
RATE_LIMIT_WINDOW_S = 60.0  # 1 minute sliding window
RATE_LIMIT_MAX_REQUESTS = 300  # local WebUI polling and normal API traffic
RATE_LIMIT_BURST = 10  # allow short bursts

# Paths exempt from rate limiting (health checks, static assets)
_RATE_EXEMPT_PATHS = frozenset({"/health", "/health/detailed", "/favicon.ico"})
_INTERNAL_PET_PATHS = frozenset({
    "/pet/state",
    "/pet/runtime/position",
    "/pet/runtime/closed",
})
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def allowed_gateway_origins() -> list[str]:
    """Return the exact browser origins allowed to control the gateway."""
    gateway_port = os.getenv("GATEWAY_PORT", "8000").strip() or "8000"
    defaults = {
        f"http://127.0.0.1:{gateway_port}",
        f"http://localhost:{gateway_port}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }
    configured = {
        item.strip().rstrip("/")
        for item in os.getenv("GATEWAY_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    return sorted(defaults | configured)


class GatewaySecurityMiddleware(BaseHTTPMiddleware):
    """Protect browser requests and require a token for remote clients.

    Loopback CLI clients may omit both Origin and token. Browser requests must
    come from an explicitly allowed local origin. Non-loopback clients always
    need ``GATEWAY_API_TOKEN`` via Bearer or ``X-SJTUClaw-Token``.
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self._allowed_origins = frozenset(allowed_gateway_origins())
        self._token = os.getenv("GATEWAY_API_TOKEN", "").strip()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _RATE_EXEMPT_PATHS or _is_internal_pet_request(request, path):
            return await call_next(request)

        # Static assets and the SPA shell are read-only. API paths are the only
        # resources that can mutate or expose local agent state.
        if request.method in {"GET", "HEAD"} and (
            path == "/"
            or path.startswith("/assets/")
            or path in {"/favicon.ico", "/claw-cat.png", "/claw-cat-transparent.png"}
        ):
            return await call_next(request)

        client_host = request.client.host if request.client else "unknown"
        is_loopback = client_host in _LOOPBACK_CLIENTS
        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin and origin not in self._allowed_origins:
            return JSONResponse(status_code=403, content={"ok": False, "error": "不受信任的请求来源。"})
        if request.method == "OPTIONS":
            return await call_next(request)

        supplied = request.headers.get("x-sjtuclaw-token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        token_ok = bool(self._token and hmac.compare_digest(supplied, self._token))

        if not is_loopback and not token_ok:
            return JSONResponse(status_code=401, content={"ok": False, "error": "Gateway API 认证失败。"})
        return await call_next(request)


def _is_internal_pet_request(request: Request, path: str) -> bool:
    """Recognise local requests sent by the out-of-process desktop pet."""
    client_host = request.client.host if request.client else "unknown"
    return (
        path in _INTERNAL_PET_PATHS
        and client_host in _LOOPBACK_CLIENTS
        and request.headers.get("x-sjtuclaw-internal") == "desktop-pet"
    )


class RateLimiter:
    """Sliding-window rate limiter with burst allowance.

    Thread-safe per-client request counter. Allows up to
    ``RATE_LIMIT_MAX_REQUESTS`` per ``RATE_LIMIT_WINDOW_S`` seconds,
    with a burst allowance of ``RATE_LIMIT_BURST`` concurrent requests.
    """

    def __init__(
        self,
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        window_s: float = RATE_LIMIT_WINDOW_S,
        burst: int = RATE_LIMIT_BURST,
    ):
        self._max_requests = max_requests
        self._window_s = window_s
        self._burst = burst
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, client_id: str) -> tuple[bool, int]:
        """Check if *client_id* may proceed.

        Returns ``(allowed, remaining)`` where *remaining* is the
        number of requests the client can still make in the current window.
        """
        now = time.monotonic()
        cutoff = now - self._window_s
        with self._lock:
            bucket = self._buckets[client_id]
            # Evict expired entries
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                remaining = 0
                return False, remaining
            bucket.append(now)
            remaining = self._max_requests - len(bucket)
            return True, remaining

    def cleanup_stale(self, max_clients: int = 10_000) -> None:
        """Evict idle client buckets to prevent unbounded memory growth."""
        now = time.monotonic()
        cutoff = now - self._window_s
        with self._lock:
            if len(self._buckets) > max_clients:
                # Too many clients — evict all empty/expired buckets
                stale = [
                    cid for cid, bucket in self._buckets.items()
                    if not bucket or bucket[-1] < cutoff
                ]
                for cid in stale:
                    del self._buckets[cid]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces per-client rate limits."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter | None = None):
        super().__init__(app)
        self._limiter = limiter or RateLimiter()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path in _RATE_EXEMPT_PATHS
            or path.startswith("/web")
            or _is_internal_pet_request(request, path)
        ):
            return await call_next(request)

        client_id = request.client.host if request.client else "unknown"
        allowed, remaining = self._limiter.check(client_id)
        if not allowed:
            logger.warning("Rate limit exceeded for client %s on %s", client_id, path)
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "error": "请求过于频繁，请稍后再试。",
                    "retryAfter": int(RATE_LIMIT_WINDOW_S),
                },
                headers={
                    "Retry-After": str(int(RATE_LIMIT_WINDOW_S)),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Limit": str(RATE_LIMIT_MAX_REQUESTS),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT_MAX_REQUESTS)
        return response


class RequestSizeMiddleware:
    """ASGI body limiter that counts actual chunks, not only Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        limit = (
            MAX_ATTACHMENT_BYTES + 1024 * 1024
            if path.endswith("/attachments") or path == "/pet/pets"
            else self._max_bytes
        )
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"")
        try:
            declared = int(raw_length) if raw_length else None
        except ValueError:
            declared = None
        if declared is not None and declared > limit:
            response = JSONResponse(status_code=413, content={"ok": False, "error": "请求体超过大小限制。"})
            await response(scope, receive, send)
            return

        consumed = 0
        response_started = False

        class _BodyTooLarge(Exception):
            pass

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            if response_started:
                raise
            response = JSONResponse(status_code=413, content={"ok": False, "error": "请求体超过大小限制。"})
            await response(scope, receive, send)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log each request with a unique ID, method, path, status, and duration."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        # Stash on state for downstream handlers / error loggers
        request.state.request_id = request_id

        start = time.perf_counter()
        method = request.method
        path = request.url.path

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request %s %s id=%s failed after %.1fms",
                method, path, request_id, duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id

        # Log slow requests at WARNING level
        log_level = logging.WARNING if duration_ms > 5000 else logging.DEBUG
        logger.log(
            log_level,
            "request %s %s id=%s status=%d duration=%.1fms",
            method, path, request_id, response.status_code, duration_ms,
        )
        return response


def sanitize_error_message(exc: Exception, include_detail: bool = False) -> str:
    """Return a user-safe error message, optionally with detail for logs.

    Strips file paths, API keys, and other sensitive information that
    might leak through exception string representations.
    """
    msg = str(exc) or exc.__class__.__name__
    if include_detail:
        return msg
    # For client-facing messages, use a generic description
    # and rely on server logs for the full detail.
    exc_name = exc.__class__.__name__
    if "Auth" in exc_name or "Key" in exc_name or "Token" in exc_name:
        return "认证失败，请检查配置。"
    if "Timeout" in exc_name or "Connection" in exc_name:
        return "网络连接超时，请稍后重试。"
    if "Permission" in exc_name or "Forbidden" in exc_name:
        return "权限不足。"
    return f"内部错误 ({exc_name})"


__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_ATTACHMENT_BYTES",
    "RATE_LIMIT_WINDOW_S",
    "RATE_LIMIT_MAX_REQUESTS",
    "allowed_gateway_origins",
    "GatewaySecurityMiddleware",
    "RateLimiter",
    "RateLimitMiddleware",
    "RequestSizeMiddleware",
    "RequestLoggingMiddleware",
    "sanitize_error_message",
]
