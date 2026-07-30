"""FastAPI middleware.

Three middlewares stack onto the FastAPI app:

1. ``RateLimitMiddleware`` applies token-bucket rate limiting to LLM-triggering
   endpoints (``/v1/messages``, ``/api/replay/*``) and admin endpoints
   (``DELETE /api/requests``, ``GET /api/providers/health``). Returns 429 when
   the bucket is empty.

2. ``BodyCaptureMiddleware`` reads POST/PUT/PATCH bodies once, eagerly, into
   ``request.state.body_bytes`` so that handlers can forward the same
   bytes to an upstream LLM provider without re-reading.

3. ``AccessLogMiddleware`` writes one ``METHOD /path status duration_ms``
   line per request to the ``app.access`` logger, coloured by status class.

All run inside Starlette's ``BaseHTTPMiddleware`` framework.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("claude_code_proxy.access")

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
# Hard ceiling on inbound request bodies. LLM API requests are JSON payloads;
# 50 MB is generous even for max-context requests with image attachments.
MAX_REQUEST_BODY_BYTES = 50 * 1024 * 1024

_GREEN = "\033[32m"
_BLUE = "\033[34m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _color_for(status: int) -> str:
    if status >= 500:
        return _RED
    if status >= 400:
        return _YELLOW
    if status >= 300:
        return _BLUE
    return _GREEN


def _make_429(rate: float) -> "Response":
    from starlette.responses import JSONResponse

    retry_after = max(1.0, rate)
    return JSONResponse(
        {"error": "rate limit exceeded", "retry_after_seconds": retry_after},
        status_code=429,
        headers={"Retry-After": str(max(1, int(retry_after)))},
    )


class _TokenBucket:
    """Simple token-bucket; not thread-safe (single-threaded asyncio loop)."""

    __slots__ = ("_max", "_tokens", "_rate", "_last")

    def __init__(self, max_tokens: int, refill_seconds: float) -> None:
        self._max = max_tokens
        self._tokens = float(max_tokens)
        self._rate = max_tokens / refill_seconds
        self._last = time.monotonic()

    def consume(self) -> bool:
        """Return True and deduct a token if available, False otherwise."""
        now = time.monotonic()
        self._tokens = min(self._max, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def retry_after(self) -> float:
        """Seconds until at least one token is available."""
        deficit = 1.0 - self._tokens
        return max(1.0, deficit / self._rate)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter for LLM-triggering and admin endpoints.

    Two buckets:
    - *LLM bucket*: ``POST /v1/messages`` and ``POST /api/replay/*``
      (both trigger upstream API calls). Default: 60 req/min.
    - *Admin bucket*: ``DELETE /api/requests`` and ``GET /api/providers/health``
      (cheap but potentially abused for DoS). Default: 30 req/min.
    """

    def __init__(
        self,
        app,
        *,
        max_tokens: int = 60,
        refill_seconds: float = 60.0,
        admin_max_tokens: int = 30,
        admin_refill_seconds: float = 60.0,
    ) -> None:
        super().__init__(app)
        self._llm_bucket = _TokenBucket(max_tokens, refill_seconds)
        self._admin_bucket = _TokenBucket(admin_max_tokens, admin_refill_seconds)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        method = request.method
        # LLM-triggering endpoints share one bucket (replay is an LLM call).
        if (method == "POST" and path == "/v1/messages") or (
            method == "POST" and path.startswith("/api/replay/")
        ):
            if not self._llm_bucket.consume():
                return _make_429(self._llm_bucket.retry_after())
        # Admin endpoints — cheaper but still worth guarding.
        elif (method == "DELETE" and path.startswith("/api/requests")) or (
            method == "GET" and path == "/api/providers/health"
        ):
            if not self._admin_bucket.consume():
                return _make_429(self._admin_bucket.retry_after())
        return await call_next(request)


class BodyCaptureMiddleware(BaseHTTPMiddleware):
    """Read the request body once and stash it on ``request.state``."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in _BODY_METHODS:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_REQUEST_BODY_BYTES:
                        from starlette.responses import JSONResponse
                        return JSONResponse(
                            {"error": "request body too large"},
                            status_code=413,
                        )
                except ValueError:
                    pass
            body = await request.body()
            if len(body) > MAX_REQUEST_BODY_BYTES:
                from starlette.responses import JSONResponse
                return JSONResponse(
                    {"error": "request body too large"},
                    status_code=413,
                )
            request.state.body_bytes = body
        return await call_next(request)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one colored access-log line per request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.monotonic()
        response: Response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        color = _color_for(response.status_code)
        logger.info(
            "%s%s %s -> %d (%.1fms)%s",
            color,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            _RESET,
        )
        return response
