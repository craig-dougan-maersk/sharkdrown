"""Provider abstract base class.

A ``Provider`` knows how to send a request to one LLM upstream and return the
raw ``httpx.Response``. The proxy handler (Phase 6) builds the request body
and decides which provider to call; the provider is responsible only for
sending it and shaping the response.

``headers`` is a mapping from the *client* (the inbound Claude Code request).
Each provider decides which of those headers to forward and which to strip or
replace. ``body`` is the already-built upstream request body — for
``AnthropicProvider`` this is the Anthropic JSON the client sent (possibly
re-marshaled with a different ``model`` after subagent routing); for
``OpenAIProvider`` this is the converted OpenAI chat-completions JSON.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx


class Provider(ABC):
    """One upstream LLM provider (Anthropic, OpenAI, …)."""

    name: str

    @abstractmethod
    async def forward(
        self,
        body: bytes,
        *,
        headers: dict[str, str],
        stream: bool = False,
        target_url: str | None = None,
    ) -> httpx.Response:
        """Send ``body`` to the upstream LLM and return the raw response.

        ``headers`` are forwarded from the client request — typically the
        ``x-api-key`` and other auth headers. The provider may strip,
        replace, or augment them as appropriate (e.g. OpenAI strips
        anthropic-* and injects an ``Authorization: Bearer``).

        ``stream=True`` returns an ``httpx.Response`` whose body is a
        byte iterator suitable for SSE forwarding. The caller is responsible
        for iterating the body to forward chunks to the client.

        ``target_url`` optionally overrides the provider's configured
        ``base_url`` for this single request, used by the per-model
        endpoint resolver in the desktop app. When ``None``, the
        provider uses its own ``base_url`` (the original behaviour).
        """
        raise NotImplementedError
