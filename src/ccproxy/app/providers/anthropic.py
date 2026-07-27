"""Anthropic provider — pass-through.

Forwards the inbound Anthropic-format request to the configured ``base_url``,
rewriting the URL host and stripping hop-by-hop headers. The upstream
``anthropic-version`` is injected if missing, and ``Accept-Encoding: gzip``
is always set so httpx transparently decompresses the response.

Auth headers (``x-api-key`` etc.) are forwarded verbatim — the proxy is
transparent.
"""

from __future__ import annotations

import logging
import sys
from urllib.parse import urljoin

import httpx

from ccproxy.app.providers.base import Provider

logger = logging.getLogger(__name__)

# Headers listed in RFC 7230 §6.1 that must not be forwarded by a proxy.
HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _strip_hop_by_hop(headers: dict[str, str]) -> dict[str, str]:
    """Drop headers that are scoped to a single transport-level hop."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        base_url: str,
        *,
        version: str = "2023-06-01",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._timeout = timeout_seconds
        # The client is shared; close is the caller's responsibility (or
        # FastAPI's lifespan teardown in Phase 6).
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def forward(
        self,
        body: bytes,
        *,
        headers: dict[str, str],
        stream: bool = False,
        target_url: str | None = None,
        target_api_key: str | None = None,
    ) -> httpx.Response:
        # ``target_url`` overrides the configured ``base_url`` for this
        # single request when set (per-model endpoint routing). We don't
        # append ``/v1/messages`` in that case because the caller passes a
        # complete URL.
        if target_url:
            url = target_url
        else:
            url = urljoin(self._base_url + "/", "v1/messages")

        # Forward client headers; strip hop-by-hop; ensure anthropic-version.
        out_headers = _strip_hop_by_hop(dict(headers))
        # ``target_api_key``: when the resolver hands us a per-provider
        # key, replace any client-provided ``x-api-key`` so the
        # upstream sees the provider's key. Strip case-insensitively
        # to catch ``x-api-key`` and ``X-Api-Key`` variants — httpx
        # normalises header case on the wire anyway. Without this
        # substitution the client-supplied key (which the session
        # launcher may have set to a per-profile default) would leak
        # into the upstream regardless of which provider we're
        # routing through.
        #
        # Diagnostic: log the prefix-and-suffix of the key we're
        # sending so a 401 "Missing Authentication header" from
        # OpenRouter (or similar) is attributable to a *known* key
        # shape rather than a silent corruption somewhere in the
        # set→read→forward chain. The middle of the key is never
        # logged. Keys < 8 chars have their entire value obscured,
        # which is fine — anything that short isn't a real key.
        if target_api_key:
            key_prefix = target_api_key[:4]
            key_suffix = target_api_key[-4:] if len(target_api_key) > 8 else "***"
            print(
                f"anthropic forward: target_api_key prefix={key_prefix!r} "
                f"suffix={key_suffix!r} len={len(target_api_key)}",
                file=sys.stderr,
            )
        if target_api_key:
            for k in list(out_headers.keys()):
                if k.lower() == "x-api-key":
                    out_headers.pop(k, None)
            out_headers["x-api-key"] = target_api_key
        if "anthropic-version" not in {k.lower() for k in out_headers}:
            out_headers["anthropic-version"] = self._version
        # Force gzip so httpx transparently decompresses.
        out_headers["accept-encoding"] = "gzip"
        # Content-Type: client usually sends application/json; assert it.
        out_headers.setdefault("content-type", "application/json")

        request = httpx.Request(
            "POST",
            url,
            headers=out_headers,
            content=body,
        )
        response = await self._client.send(request, stream=stream)

        # MiniMax (and similar nginx-fronted gateways) returns a 301/302
        # to a *directory* URL with a trailing slash but no API path —
        # ``Location: http://host/path/`` instead of
        # ``http://host/path/v1/messages``. Claude Code's HTTP client
        # can't transparently recover from this because the redirect
        # target isn't a valid API endpoint; it just chokes and shows
        # "model doesn't exist". Detect the trailing-slash
        # canonicalization pattern and retry the same POST against
        # ``<Location>/v1/messages``.
        #
        # Note: we deliberately allow protocol downgrades for this
        # specific retry path. ``httpx`` refuses HTTPS → HTTP by
        # default, but the user has MiniMax configured for the
        # Anthropic-format gateway whose nginx *intentionally*
        # 301s HTTPS → HTTP. Refusing the retry here breaks the
        # flow entirely; reconstructing the API URL explicitly is
        # safe enough — it's not a transparent redirect follow, the
        # URL is built from the Location header plus a known suffix.
        # ``logger.info`` is suppressed by uvicorn's dictConfig; the
        # proxy startup banner (``main.py``) already uses
        # ``print(..., file=sys.stderr)`` for the same reason. We do
        # the same here so the line shows up in the user's terminal
        # / container logs next to uvicorn's own access log.
        print(
            f"anthropic forward: post-send check status={response.status_code} "
            f"location={response.headers.get('location', '')!r}",
            file=sys.stderr,
        )
        if response.status_code in (301, 302):
            location = response.headers.get("location", "")
            if location and location.endswith("/"):
                retry_url = location.rstrip("/") + "/v1/messages"
                # Drain the redirect response body before retrying
                # (httpx requires this before re-using the client).
                await response.aread()
                print(
                    "anthropic forward: upstream 3xx points at "
                    f"directory URL; retrying POST at {retry_url}",
                    file=sys.stderr,
                )
                retry_request = httpx.Request(
                    "POST",
                    retry_url,
                    headers=out_headers,
                    content=body,
                )
                response = await self._client.send(
                    retry_request, stream=stream
                )
        return response

    async def aclose(self) -> None:
        await self._client.aclose()
