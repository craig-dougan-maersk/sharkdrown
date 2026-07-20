"""FastAPI middleware.

Two middlewares stack onto the FastAPI app:

1. ``BodyCaptureMiddleware`` reads POST/PUT/PATCH bodies once, eagerly, into
   ``request.state.body_bytes`` so that handlers can forward the same
   bytes to an upstream LLM provider without re-reading.

2. ``AccessLogMiddleware`` writes one ``METHOD /path status duration_ms``
   line per request to the ``app.access`` logger, coloured by status class.

Both run inside Starlette's ``BaseHTTPMiddleware`` framework.
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


class BodyCaptureMiddleware(BaseHTTPMiddleware):
    """Read the request body once and stash it on ``request.state``."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in _BODY_METHODS:
            body = await request.body()
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
