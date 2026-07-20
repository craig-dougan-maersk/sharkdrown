"""The main proxy routes — /v1/messages, /v1/models, /v1/chat/completions.

The proxy flow:

    POST /v1/messages
        1. Read body (already buffered by BodyCaptureMiddleware)
        2. Parse to AnthropicRequest (Pydantic)
        3. Generate request_id
        4. SubagentRouter picks provider + target model
        5. Re-marshal body if the target model differs
        6. Save RequestLog (without response) to SQLite
        7. Call provider.forward(body, headers, stream=req.stream)
        8. Branch:
            - streaming  → return StreamingResponse that proxies bytes and
                           captures the chunks for storage
            - non-stream → read body, build ResponseLog, return Response
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ccproxy.app.models.anthropic import AnthropicRequest
from ccproxy.app.models.storage import RequestLog, ResponseLog
from ccproxy.app.providers.openai import transform_openai_response_to_anthropic
from ccproxy.app.utils.headers import sanitize_headers
from ccproxy.app.utils.ids import generate_request_id


def sanitize_headers_dict(headers: Any) -> dict[str, list[str]]:
    """Sanitize Starlette ``Headers`` (which has multi-values per key) into the
    ``dict[str, list[str]]`` shape the storage layer expects.
    """
    out: dict[str, list[str]] = {}
    for key in headers:
        values = headers.getlist(key)
        sanitized = sanitize_headers({key: values[0] if values else ""})
        out[key] = [sanitized[key]]
    return out


logger = logging.getLogger("claude_code_proxy.api.proxy")

router = APIRouter()


# Anthropic-shaped JSON error envelope. The Claude Code client understands
# this shape; anything else gets parsed as a generic error.
_ERROR_ENVELOPE = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "POST /v1/chat/completions is not supported by this proxy. Use /v1/messages instead.",
    },
}


@router.post("/v1/chat/completions")
async def chat_completions_rejected() -> JSONResponse:
    """The proxy only handles /v1/messages (Anthropic API)."""
    return JSONResponse(_ERROR_ENVELOPE, status_code=400)


@router.get("/v1/models")
async def list_models() -> JSONResponse:
    """Empty list — the proxy doesn't track or expose a model catalog."""
    return JSONResponse({"object": "list", "data": []})


@router.post("/v1/messages")
async def post_v1_messages(request: Request) -> Response:
    """Forward an Anthropic-format request to the configured provider."""
    body: bytes = request.state.body_bytes  # populated by BodyCaptureMiddleware

    try:
        req = AnthropicRequest.model_validate_json(body)
    except Exception as e:
        logger.warning("invalid /v1/messages body: %s", e)
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": f"Could not parse Anthropic request: {e}",
                },
            },
            status_code=400,
        )

    storage = request.app.state.storage
    router_obj = request.app.state.router

    request_id = generate_request_id()
    decision = router_obj.determine_route(req)

    # Build the upstream body.
    # - OpenAI routing: always convert Anthropic→OpenAI (system flatten, tool
    #   conversion, o-series quirks, etc.). The conversion lives in the OpenAI
    #   provider module so it can be unit-tested independently.
    # - Anthropic routing with a model override: re-marshal with the new model.
    # - Anthropic routing without override: forward verbatim.
    from ccproxy.app.providers.openai import convert_anthropic_to_openai

    upstream_body = body
    if decision.target_provider_name == "openai":
        openai_body = convert_anthropic_to_openai(
            req.model_copy(update={"model": decision.target_model})
        )
        upstream_body = json.dumps(openai_body, ensure_ascii=False).encode()
    elif decision.target_model != req.model:
        req = req.model_copy(update={"model": decision.target_model})
        upstream_body = req.model_dump_json(exclude_none=True).encode()

    # Persist the request *before* forwarding.
    sanitized_headers = sanitize_headers_dict(request.headers)
    request_log = RequestLog(
        id=request_id,
        timestamp=datetime.now(UTC),
        method="POST",
        endpoint="/v1/messages",
        original_model=req.model
        if decision.target_model != decision.original_model
        else decision.original_model,
        routed_model=decision.target_model,
        routed_provider=decision.target_provider_name,
        request_path=decision.path,
        user_agent=request.headers.get("user-agent"),
        content_type=request.headers.get("content-type", "application/json"),
        headers=sanitized_headers,
        body=req.model_dump(mode="json"),
    )
    logger.warning("DEBUG: saving request %s", request_id)
    await storage.save_request(request_log)
    logger.warning("DEBUG: saved request %s", request_id)

    # Build the outbound headers from the inbound client headers, filtering
    # out our middleware-added / framework headers.
    outbound_headers = _client_headers(dict(request.headers))

    # Per-model endpoint resolution. Look up the *requested* model in
    # the configured mapping first; fall back to the default endpoint
    # if set; finally fall back to the provider's own base_url (the
    # original behaviour). We resolve against the original model name
    # (not the routed/target name) so users can map Claude Code's model
    # name (e.g. "claude-sonnet-4-5") to a specific upstream.
    target_url: str | None = None
    endpoints_cfg = request.app.state.settings.model_endpoints
    if endpoints_cfg.endpoints:
        target_url = endpoints_cfg.endpoints.get(req.model)
    if target_url is None and endpoints_cfg.default_endpoint:
        target_url = endpoints_cfg.default_endpoint

    response = await decision.provider.forward(
        upstream_body,
        headers=outbound_headers,
        stream=req.stream,
        target_url=target_url,
    )

    if req.stream:
        # Streaming: proxy bytes through, capture chunks, persist ResponseLog
        # on completion.
        from ccproxy.app.streaming.capture import stream_and_capture

        async def stream_iter() -> AsyncIterator[bytes]:
            async for chunk in stream_and_capture(
                response, storage, request_id, request_log.endpoint
            ):
                yield chunk

        return StreamingResponse(
            stream_iter(),
            status_code=response.status_code,
            headers=_filter_response_headers(dict(response.headers)),
            media_type="text/event-stream",
        )

    # Non-streaming: read the body, possibly convert OpenAI→Anthropic,
    # persist ResponseLog, and return.
    raw_body = await response.aread()
    body_out = raw_body

    if decision.target_provider_name == "openai":
        # Convert the OpenAI chat-completion JSON to Anthropic-message JSON.
        body_out = transform_openai_response_to_anthropic(raw_body)

    response_log = ResponseLog(
        status_code=response.status_code,
        headers=dict(response.headers.items()),
        body_text=body_out.decode("utf-8", errors="replace"),
        is_streaming=False,
        response_time_ms=None,
    )
    await storage.update_request_with_response(request_id, response_log)

    return Response(
        content=body_out,
        status_code=response.status_code,
        headers=_filter_response_headers(dict(response.headers)),
        media_type=response.headers.get("content-type", "application/json"),
    )


# ---- Helpers ---------------------------------------------------------------


def _client_headers(headers: dict[str, str]) -> dict[str, str]:
    """Keep client-relevant headers; strip middleware/internal ones."""
    skip = {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in skip:
            continue
        out[k] = v
    return out


def _filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop hop-by-hop and content-length headers from an upstream response."""
    skip = {
        "content-length",
        "content-encoding",  # httpx already decompressed; don't double-encode
        "transfer-encoding",
        "connection",
    }
    return {k: v for k, v in headers.items() if k.lower() not in skip}
