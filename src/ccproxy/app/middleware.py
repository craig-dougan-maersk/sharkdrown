"""FastAPI middleware.

Three middlewares stack onto the FastAPI app:

1. ``RateLimitMiddleware`` applies a token-bucket rate limiter to the
   ``/v1/messages`` endpoint. Returns 429 when the bucket is empty.

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


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter for the proxy endpoint.

    Prevents accidental infinite loops (e.g. a misconfigured agent that
    retries immediately on failure) from flooding the upstream API and
    exhausting quotas. Only applies to POST /v1/messages.

    Defaults: 60 requests per minute, bucket refills continuously.
    """

    def __init__(self, app, *, max_tokens: int = 60, refill_seconds: float = 60.0) -> None:
        super().__init__(app)
        self._max_tokens = max_tokens
        self._tokens = float(max_tokens)
        self._refill_rate = max_tokens / refill_seconds
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path == "/v1/messages" and request.method == "POST":
            self._refill()
            if self._tokens < 1.0:
                from starlette.responses import JSONResponse

                # Compute how long until the bucket refills enough for 1 token.
                deficit = 1.0 - self._tokens
                retry_after = max(1.0, deficit / self._refill_rate)
                retry_after_int = max(1, int(retry_after))
                return JSONResponse(
                    {"error": "rate limit exceeded", "retry_after_seconds": retry_after},
                    status_code=429,
                    headers={"Retry-After": str(retry_after_int)},
                )
            self._tokens -= 1.0
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
