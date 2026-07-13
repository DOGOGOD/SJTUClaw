"""Gateway middleware: rate limiting, request logging, request size validation.

Gateway middleware providing:
- Request size cap (MAX_REQUEST_BYTES)
- Per-client rate limiting
- Structured request logging with request IDs
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

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


class RequestSizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds ``max_bytes``."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES):
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
                if size > self._max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "ok": False,
                            "error": (
                                f"请求体过大 ({size} bytes)，"
                                f"上限为 {self._max_bytes} bytes。"
                            ),
                        },
                    )
            except ValueError:
                pass
        return await call_next(request)


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
    "RateLimiter",
    "RateLimitMiddleware",
    "RequestSizeMiddleware",
    "RequestLoggingMiddleware",
    "sanitize_error_message",
]
