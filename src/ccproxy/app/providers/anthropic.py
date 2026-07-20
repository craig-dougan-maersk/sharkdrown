"""Anthropic provider — pass-through.

Forwards the inbound Anthropic-format request to the configured ``base_url``,
rewriting the URL host and stripping hop-by-hop headers. The upstream
``anthropic-version`` is injected if missing, and ``Accept-Encoding: gzip``
is always set so httpx transparently decompresses the response.

Auth headers (``x-api-key`` etc.) are forwarded verbatim — the proxy is
transparent.
"""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from ccproxy.app.providers.base import Provider

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
        return await self._client.send(request, stream=stream)

    async def aclose(self) -> None:
        await self._client.aclose()
