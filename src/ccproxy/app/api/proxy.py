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
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ccproxy.app.config import ModelEndpointsCfg, Settings
from ccproxy.app.db.storage import StorageService
from ccproxy.app.middleware import MAX_REQUEST_BODY_BYTES
from ccproxy.app.models.anthropic import AnthropicRequest
from ccproxy.app.models.storage import RequestLog, ResponseLog
from ccproxy.app.providers.openai import transform_openai_response_to_anthropic
from ccproxy.app.router.subagent import SubagentRouter
from ccproxy.app.utils.headers import sanitize_headers
from ccproxy.app.utils.ids import generate_request_id


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request) -> StorageService:
    return request.app.state.storage


def get_router(request: Request) -> SubagentRouter:
    return request.app.state.router


def sanitize_headers_dict(headers: Any) -> dict[str, list[str]]:
    """Sanitize Starlette ``Headers`` (which has multi-values per key) into the
    ``dict[str, list[str]]`` shape the storage layer expects.
    """
    out: dict[str, list[str]] = {}
    for key in headers:
        values = headers.getlist(key)
        out[key] = [sanitize_headers({key: v})[key] for v in values]
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


def _apply_alias_rewrite(
    req: AnthropicRequest, alias_map: dict[str, str]
) -> tuple[AnthropicRequest, str | None]:
    """Rewrite ``req.model`` if it's a configured alias.

    Returns a ``(new_req, incoming_alias)`` tuple:
    - ``new_req``: the rewritten request (or the original if no
      rewrite happened).
    - ``incoming_alias``: the alias the user actually sent (or
      ``None`` if they sent the real model name directly).

    The alias is preserved separately so the request log can show
    what was actually requested, even though the router / URL lookup
    operate on the rewritten name.
    """
    if alias_map and req.model in alias_map:
        incoming_alias = req.model
        return (
            req.model_copy(update={"model": alias_map[req.model]}),
            incoming_alias,
        )
    return req, None


def _extract_incoming_model(body: bytes) -> str | None:
    """Best-effort extract the ``model`` field from a raw request body.

    Used by the ``/v1/messages`` entry-log so the log line shows
    the model name *before* the body is parsed. Returns ``None`` for
    bodies that aren't valid JSON or lack a ``model`` key — both
    cases surface in the parse-failure path right after.
    """
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict):
        m = parsed.get("model")
        if isinstance(m, str):
            return m
    return None


def _resolve_request_target(
    model: str,
    endpoints_cfg: ModelEndpointsCfg,
) -> tuple[str | None, str | None]:
    """Resolve the upstream URL and API key for ``model``.

    Returns ``(target_url, target_api_key)`` — either may be ``None``
    when nothing resolves. Resolution order:

    1. ``endpoints_cfg.endpoints[model]`` → ``provider_name``
    2. ``endpoints_cfg.providers[provider_name]`` → ``(url, api_key)``
    3. Falls back to ``endpoints_cfg.default_provider`` if ``model``
       isn't in ``endpoints``.
    4. Returns ``(None, None)`` when nothing resolves so the caller
       can apply the final ``default_endpoint`` URL backstop.

    The resolver deliberately doesn't fall back to ``default_endpoint``
    itself — that's a URL with no auth identity, kept around for
    legacy YAML configs that haven't migrated. The proxy handler
    chains that backstop on top of this resolver.
    """
    provider_name = endpoints_cfg.endpoints.get(model) or ""
    if not provider_name:
        provider_name = endpoints_cfg.default_provider or ""
    if not provider_name:
        return (None, None)
    provider = endpoints_cfg.providers.get(provider_name)
    if provider is None:
        return (None, None)
    return (
        provider.url or None,
        provider.api_key.get_secret_value() if provider.api_key else None,
    )


@router.get("/v1/models")
async def list_models(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Return the proxy's view of its routing config.

    Anthropic-shaped envelope (``{"object": "list", "data": [...]}``)
    so Claude Code's ``/model`` picker has somewhere to introspect,
    with a debug ``__proxy__`` field the proxy uses to surface its
    own configuration. ``GET /v1/models`` doesn't write to the
    request log; this route is purely for diagnostics.

    The output makes it obvious when ``freestuff → real-model``
    alias rewrite is configured (or not), which is the easiest
    way to verify why a request reaches (or bypasses) the proxy.

    Query flags:
      ``?debug=1`` → include the ``__proxy__`` block. Default off
      because Claude Code treats ``/v1/models`` as a model
      catalog and would silently drop unknown fields.
    """
    endpoints_cfg = settings.model_endpoints
    alias_map = endpoints_cfg.model_aliases or {}
    # Build an inverse map (real name → aliases) so the response can
    # show "MiniMax-M3 has aliases [opus, team-pro]" — useful for
    # verifying the proxy knows about each endpoint.
    real_to_aliases: dict[str, list[str]] = {}
    for alias, real in alias_map.items():
        real_to_aliases.setdefault(real, []).append(alias)
    models: list[dict] = []
    seen: set[str] = set()
    for real, aliases in real_to_aliases.items():
        models.append(
            {"id": real, "aliases": sorted(aliases)}
        )
        seen.add(real)
    for real in (endpoints_cfg.endpoints or {}):
        if real in seen:
            continue
        models.append({"id": real, "aliases": []})
    debug_requested = "debug" in request.query_params
    debug_token = getattr(request.app.state, "debug_token", None)
    provided_token = request.query_params.get("token", "")
    debug = debug_requested and debug_token is not None and provided_token == debug_token
    payload: dict = {"object": "list", "data": models}
    if debug:
        payload["__proxy__"] = {
            "providers": {
                name: {
                    "url": p.url,
                    "has_api_key": bool(p.api_key),
                    "available_models": list(p.available_models or []),
                }
                for name, p in (endpoints_cfg.providers or {}).items()
            },
            "default_provider": endpoints_cfg.default_provider,
            "default_endpoint": endpoints_cfg.default_endpoint,
            "alias_map": dict(alias_map),
            "endpoints_map": dict(endpoints_cfg.endpoints or {}),
        }
    return JSONResponse(payload)


@router.post("/v1/messages")
async def post_v1_messages(
    request: Request,
    settings: Settings = Depends(get_settings),
    storage: StorageService = Depends(get_storage),
    router_obj: SubagentRouter = Depends(get_router),
) -> Response:
    """Forward an Anthropic-format request to the configured provider.

    Every incoming ``POST /v1/messages`` is logged at INFO level
    before any other work runs, *before* the body is parsed, so a
    missing parse or a 400 response still leaves a paper trail in
    the desktop app's log. The pre-parse log line is what users see
    when their session reports an upstream error but Recent Activity
    is empty — the request reached us, parsing or routing bailed
    silently.
    """
    body: bytes = getattr(request.state, "body_bytes", b"")  # set by BodyCaptureMiddleware
    # ``logger.info`` is suppressed by uvicorn's dictConfig; the
    # startup banner (``main.py``) already uses ``print(..., file=sys.stderr)``
    # for the same reason. We do the same here so the line shows up
    # in the user's terminal / container logs next to uvicorn's
    # own access log.
    print(
        f"/v1/messages hit: incoming_model={_extract_incoming_model(body)!r} "
        f"anthropic-version={request.headers.get('anthropic-version')!r} "
        f"content-length={len(body)} "
        f"ua={request.headers.get('user-agent')!r}",
        file=sys.stderr,
    )

    try:
        req = AnthropicRequest.model_validate_json(body)
    except (ValueError, TypeError) as e:
        logger.warning(
            "invalid /v1/messages body: %s — returning 400 without "
            "logging a request record (body parse failed before "
            "save_request runs)",
            e,
        )
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

    # Alias rewrite. Resolve an incoming alias (e.g. "opus") to its
    # real model name ("claude-opus-4-8") before the router runs,
    # so subagent detection and URL lookup see the real name. The
    # alias is preserved on ``request.state.incoming_alias`` so the
    # request log below can show what the user actually sent.
    alias_map: dict[str, str] = (
        settings.model_endpoints.model_aliases or {}
    )
    req, incoming_alias = _apply_alias_rewrite(req, alias_map)
    request.state.incoming_alias = incoming_alias

    request_id = generate_request_id()
    decision = router_obj.determine_route(req)

    # Build the upstream body.
    # - OpenAI routing: always convert Anthropic→OpenAI (system flatten, tool
    #   conversion, o-series quirks, etc.). The conversion lives in the OpenAI
    #   provider module so it can be unit-tested independently.
    # - Anthropic routing with a model override: re-marshal with the new model.
    # - Anthropic routing with an alias rewrite: re-marshal with the rewritten
    #   model name. The alias-rewrite step above updated ``req.model`` in
    #   memory but the wire body still has the alias — without an explicit
    #   re-marshal here, OpenRouter (and others) reject the request with
    #   "``freestuff`` is not a valid model ID" because the alias, not the
    #   rewritten name, is what reaches the upstream.
    # - Anthropic routing with no changes: forward verbatim.
    from ccproxy.app.providers.openai import convert_anthropic_to_openai

    upstream_body = body
    alias_or_override = (
        incoming_alias is not None or decision.target_model != req.model
    )
    if decision.target_provider_name == "openai":
        openai_body = convert_anthropic_to_openai(
            req.model_copy(update={"model": decision.target_model})
        )
        upstream_body = json.dumps(openai_body, ensure_ascii=False).encode()
    elif alias_or_override:
        req = req.model_copy(update={"model": decision.target_model})
        upstream_body = req.model_dump_json(exclude_none=True).encode()

    # Persist the request *before* forwarding.
    sanitized_headers = sanitize_headers_dict(request.headers)
    # If the user sent an alias (e.g. "opus"), surface that in
    # ``original_model`` so the Recent Activity view shows what was
    # actually requested. Without this, the alias is invisible
    # because the router / URL lookup already see the real name.
    request_log = RequestLog(
        id=request_id,
        timestamp=datetime.now(UTC),
        method="POST",
        endpoint="/v1/messages",
        original_model=incoming_alias or (
            req.model
            if decision.target_model != decision.original_model
            else decision.original_model
        ),
        routed_model=decision.target_model,
        routed_provider=decision.target_provider_name,
        request_path=decision.path,
        user_agent=request.headers.get("user-agent"),
        content_type=request.headers.get("content-type", "application/json"),
        headers=sanitized_headers,
        body=req.model_dump(mode="json"),
    )
    await storage.save_request(request_log)

    # Build the outbound headers from the inbound client headers, filtering
    # out our middleware-added / framework headers.
    outbound_headers = _client_headers(dict(request.headers))

    # Per-model endpoint resolution. ``_resolve_request_target``
    # walks ``endpoints[model] → providers[name] → (url, api_key)``
    # (with ``default_provider`` as a fallback for unknown models).
    # We resolve against the post-rewrite model name so the alias
    # rewrite at the top of this handler participates in routing —
    # a client sending ``"opus"`` rewrites to ``"claude-opus-4-8"``
    # which is then mapped to its provider via the same lookup.
    endpoints_cfg = settings.model_endpoints
    target_url, target_api_key = _resolve_request_target(req.model, endpoints_cfg)
    # ``default_endpoint`` is the legacy-URL backstop: when neither
    # the explicit mapping nor the default provider resolve, point
    # the request at the URL the user previously configured. No
    # auth flows this way (the legacy shape carried no provider
    # identity to attach a key to).
    if target_url is None and endpoints_cfg.default_endpoint:
        target_url = endpoints_cfg.default_endpoint
    # ``AnthropicProvider.forward(..., target_url=...)`` uses the
    # ``target_url`` verbatim — it doesn't append ``/v1/messages``
    # to it the way it does for the built-in base URL. So if the
    # user configured a base URL (e.g. ``https://api.minimax.io/
    # anthropic``) we need to append the API path here. Skip when
    # the URL already ends with ``/v1/messages`` (a user might
    # have set the full URL explicitly for a non-Anthropic gateway).
    if target_url:
        # Append /v1/messages only when the URL doesn't already end with that
        # exact path segment pair. Use URL-path component matching rather than
        # a string suffix so that a URL like
        # https://api.example.com/v1/messages/extra doesn't pass the check and
        # then receive a doubled suffix.
        _parsed_path = PurePosixPath(urlparse(target_url).path.rstrip("/"))
        _ends_with_v1_messages = (
            len(_parsed_path.parts) >= 2
            and _parsed_path.parts[-2] == "v1"
            and _parsed_path.parts[-1] == "messages"
        )
        if not _ends_with_v1_messages:
            target_url = target_url.rstrip("/") + "/v1/messages"

    # Log the resolved route at INFO — handy when Recent Activity
    # is empty but Claude Code still sees upstream errors; this
    # line surfaces *why* a request reached (or bypassed) the
    # upstream, even when the upstream call itself fails. We use
    # ``print(..., file=sys.stderr)`` because uvicorn's dictConfig
    # suppresses module-level ``logger.info`` calls — see the same
    # pattern in the ``/v1/messages hit`` log above.
    print(
        f"/v1/messages resolved: incoming={incoming_alias or req.model!r} "
        f"rewritten={req.model!r} routed_model={decision.target_model!r} "
        f"provider={decision.target_provider_name!r} "
        f"target_url={target_url or '(NONE — would fall back to provider base_url)'!r} "
        f"has_api_key={bool(target_api_key)}",
        file=sys.stderr,
    )

    try:
        response = await decision.provider.forward(
            upstream_body,
            headers=outbound_headers,
            stream=req.stream,
            target_url=target_url,
            target_api_key=target_api_key,
        )
    except Exception as exc:  # noqa: BLE001 — surface to the caller
        logger.error(
            "/v1/messages forward failed: %s "
            "(request %s was logged above; this error happened *after* "
            "save_request, so Recent Activity should still show the row)",
            exc,
            request_id,
        )
        # Update the request log with the error so the dashboard sees
        # what happened. ``storage.update_request_with_error`` is a
        # narrow path that doesn't break older SQLite schemas.
        try:
            await storage.update_request_with_response(
                request_id,
                ResponseLog(
                    status_code=502,
                    headers={"x-proxy-error": "1"},
                    body_text=f"forward failed: {exc}".encode("utf-8").decode(
                        "utf-8", errors="replace"
                    ),
                    is_streaming=False,
                    response_time_ms=None,
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        raise

    if req.stream:
        # Streaming: proxy bytes through, capture chunks, persist ResponseLog
        # on completion.
        from ccproxy.app.streaming.capture import stream_and_capture

        async def stream_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream_and_capture(
                    response, storage, request_id, request_log.endpoint
                ):
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "streaming failed mid-response for request %s: %s",
                    request_id,
                    exc,
                )

        return StreamingResponse(
            stream_iter(),
            status_code=response.status_code,
            headers=_filter_response_headers(dict(response.headers)),
            media_type="text/event-stream",
        )

    # Non-streaming: read the body, possibly convert OpenAI→Anthropic,
    # persist ResponseLog, and return.
    raw_body = await response.aread()
    if len(raw_body) > MAX_REQUEST_BODY_BYTES:
        logger.warning(
            "upstream response for request %s exceeds size limit (%d bytes); truncating storage",
            request_id,
            len(raw_body),
        )
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
