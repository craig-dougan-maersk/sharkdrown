"""Dashboard routes — Jinja2 templates + htmx partials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from ccproxy.app.conversations.reader import ConversationReader
from ccproxy.app.models.anthropic import AnthropicRequest, AnthropicResponse
from ccproxy.app.models.storage import ResponseLog

router = APIRouter()


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    """Render a Jinja2 template by name using ``app.state.templates``."""
    template = request.app.state.templates.get_template(name)
    return HTMLResponse(template.render(context))


# Filter params are boolean with a *default*. Defaults come from the call
# site, not from the URL — checked controls send "1" or "0" explicitly so
# the URL is fully self-describing and bookmarkable.
_FILTER_DEFAULTS = {
    "hide_failed": True,      # hide 4xx/5xx by default
    "hide_no_tokens": True,   # hide rows with no token accounting
    "hide_empty": True,       # hide conversations with 0 messages (e.g. subagent spawns)
}


def _filter_bool(name: str, value: str | None) -> bool:
    """Resolve a checkbox-style query param against its declared default.

    - absent / empty -> default
    - "0" / "false" (case-insensitive) -> False
    - anything else -> True
    """
    if value is None or value == "":
        return _FILTER_DEFAULTS[name]
    return value.strip().lower() not in ("0", "false")


def _build_headers_view(headers: dict[str, list[str]]) -> list[dict[str, object]]:
    """Flatten ``{name: [values]}`` into one row per (name, value) pair,
    tagging sanitised values (those starting with ``sha256:``) so the
    template can render a ``[hashed]`` indicator next to them."""
    rows: list[dict[str, object]] = []
    for name, values in headers.items():
        for v in values:
            rows.append(
                {
                    "name": name,
                    "value": v,
                    "is_hashed": bool(v.startswith("sha256:")),
                }
            )
    return rows


def _build_request_view(body: dict[str, Any]) -> dict[str, Any]:
    """Try to parse the body as ``AnthropicRequest``; if that succeeds,
    return the parsed object so the template can render it as a
    conversation. Otherwise return ``{"shape": "unknown", "raw": body}``
    so the template falls back to formatted JSON."""
    try:
        req = AnthropicRequest.model_validate(body)
    except ValidationError:
        return {"shape": "unknown", "raw": body}
    return {"shape": "anthropic", "anthropic": req, "raw": body}


def _build_response_view(response: ResponseLog | None) -> dict[str, Any]:
    """Build a structured view of the captured response.

    Returns a dict with a ``shape`` discriminator and shape-specific
    payload:
      - ``none``: no response captured.
      - ``empty``: response captured but body_text is empty.
      - ``streaming``: streaming response — payload has ``chunks`` and
        ``assembled_text``.
      - ``anthropic``: non-streaming response parsed as ``AnthropicResponse`` —
        payload has the parsed object.
      - ``unknown``: body_text is present but not Anthropic-shaped — payload
        is the raw text.
    """
    if response is None:
        return {"shape": "none"}
    if response.is_streaming:
        return {
            "shape": "streaming",
            "chunks": response.streaming_chunks,
            "assembled_text": response.assembled_text,
        }
    if not response.body_text:
        return {"shape": "empty"}
    try:
        resp = AnthropicResponse.model_validate_json(response.body_text)
    except (ValidationError, json.JSONDecodeError, ValueError):
        return {"shape": "unknown", "raw": response.body_text}
    return {"shape": "anthropic", "anthropic": resp, "raw": response.body_text}


@router.get("/")
async def dashboard(
    request: Request,
    view: str = Query("requests"),
    model: str | None = Query(None),
    hide_failed: str | None = Query(None),
    hide_no_tokens: str | None = Query(None),
) -> HTMLResponse:
    """Full dashboard page. The view-mode toggle preserves ``view`` in the URL.

    Filter booleans (``hide_failed``, ``hide_no_tokens``) carry default-ON
    semantics: absent or empty resolves to the default, ``"0"`` flips it
    off, anything else keeps it on.
    """
    return _render(
        request,
        "dashboard/index.html",
        {
            "view": view,
            "model_filter": model or "",
            "hide_failed": _filter_bool("hide_failed", hide_failed),
            "hide_no_tokens": _filter_bool("hide_no_tokens", hide_no_tokens),
        },
    )


@router.get("/partials/stats")
async def partials_stats(request: Request) -> HTMLResponse:
    """Summary stats bar loaded via htmx."""
    from ccproxy.app.pricing import compute_cost

    storage = request.app.state.storage
    total = await storage.count_total()
    token_rows = await storage.get_token_summary()
    total_cost = 0.0
    for row in token_rows:
        cost = compute_cost(row["model"] or "unknown", row["input_tokens"], row["output_tokens"])
        if cost is not None:
            total_cost += cost
    last_ts, last_model = await storage.last_request_info()
    return _render(
        request,
        "dashboard/stats_bar.html",
        {"total": total, "total_cost_usd": total_cost, "last_model": last_model},
    )


@router.get("/partials/requests")
async def partials_requests(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    model: str | None = Query(None),
    hide_failed: str | None = Query(None),
    hide_no_tokens: str | None = Query(None),
) -> HTMLResponse:
    """List fragment for htmx. Renders the request-list rows + Load-more."""
    storage = request.app.state.storage
    hide_failed_b = _filter_bool("hide_failed", hide_failed)
    hide_no_tokens_b = _filter_bool("hide_no_tokens", hide_no_tokens)
    model_filter = model if model and model != "all" else None
    rows, total = await storage.get_requests(
        page=page,
        limit=limit,
        model_filter=model_filter,
        hide_failed=hide_failed_b,
        hide_no_tokens=hide_no_tokens_b,
    )
    has_more = page * limit < total
    return _render(
        request,
        "dashboard/request_list.html",
        {
            "rows": rows,
            "model_filter": model_filter,
            "hide_failed": hide_failed_b,
            "hide_no_tokens": hide_no_tokens_b,
            "has_more": has_more,
            "next_page": page + 1,
        },
    )


@router.get("/partials/request/{request_id}")
async def partials_request_detail(request_id: str, request: Request) -> HTMLResponse:
    """One request's detail, returned as a modal fragment."""
    storage = request.app.state.storage
    rec = await storage.get_request_by_id(request_id)
    if rec is None:
        return HTMLResponse("<p>Request not found.</p>", status_code=404)
    return _render(
        request,
        "dashboard/request_detail.html",
        {
            "request": rec,
            "headers_view": _build_headers_view(rec.headers),
            "request_view": _build_request_view(rec.body),
            "response_view": _build_response_view(rec.response),
        },
    )


@router.get("/partials/conversations")
async def partials_conversations(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    hide_empty: str | None = Query(None),
) -> HTMLResponse:
    """List fragment for the Conversations view.

    ``hide_empty`` defaults to True — subagent spawns produce many
    JSONL files with zero messages, which would otherwise dominate the
    list. Toggling the checkbox in the UI round-trips a 0/1 string.
    """
    base_path = _conversations_base(request)
    reader = ConversationReader(base_path=base_path)
    hide_empty_b = _filter_bool("hide_empty", hide_empty)
    rows, total = reader.list_conversations(
        page=page, limit=limit, hide_empty=hide_empty_b
    )
    has_more = page * limit < total
    return _render(
        request,
        "dashboard/conversation_list.html",
        {
            "rows": rows,
            "has_more": has_more,
            "next_page": page + 1,
            "hide_empty": hide_empty_b,
        },
    )


@router.get("/partials/conversation/{session_id}")
async def partials_conversation_detail(
    session_id: str,
    request: Request,
    project: str = Query(...),
) -> HTMLResponse:
    """One conversation's full messages, as a modal fragment."""
    base_path = _conversations_base(request)
    reader = ConversationReader(base_path=base_path)
    conv = reader.get_conversation(session_id, project)
    if conv is None:
        return HTMLResponse("<p>Conversation not found.</p>", status_code=404)
    return _render(request, "dashboard/conversation_detail.html", {"conv": conv})


@router.get("/partials/conversations/search")
async def partials_conversation_search(
    request: Request,
    q: str = Query("", min_length=0),
) -> HTMLResponse:
    """Search results fragment for htmx."""
    if len(q) < 2:
        return HTMLResponse("")
    base_path = _conversations_base(request)
    reader = ConversationReader(base_path=base_path)
    results = reader.search(q, limit=50)
    return _render(
        request,
        "dashboard/search_results.html",
        {"results": results, "query": q},
    )


def _conversations_base(request: Request) -> Path | None:
    """Override hook for tests."""
    override = getattr(request.app.state, "conversations_base_path", None)
    return Path(override) if override is not None else None


@router.post("/api/replay/{request_id}")
async def replay_request(request_id: str, request: Request):
    """Re-send a captured request through the proxy pipeline.

    Fetches the original request body and headers from SQLite, then
    forwards through the same routing logic as a fresh POST /v1/messages.
    Returns the new response (or an error envelope if the original
    request can't be found or replayed).
    """
    from fastapi.responses import JSONResponse

    storage = request.app.state.storage
    original = await storage.get_request_by_id(request_id)
    if original is None:
        original = await storage.get_request_by_short_id(request_id)
    if original is None:
        return JSONResponse(
            {"error": f"request {request_id!r} not found"}, status_code=404
        )
    if not original.body:
        return JSONResponse(
            {"error": "request has no body to replay"}, status_code=400
        )

    import httpx

    body_bytes = json.dumps(original.body).encode("utf-8")
    replay_headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    for key, values in (original.headers or {}).items():
        lower = key.lower()
        if lower == "anthropic-version":
            replay_headers[lower] = values[0] if values else ""
        elif lower == "x-api-key":
            stored = values[0] if values else ""
            if stored.startswith("sha256:"):
                return JSONResponse(
                    {
                        "error": "Cannot replay: stored API key is redacted. "
                                 "Configure live credentials in the active profile to replay."
                    },
                    status_code=422,
                )
            replay_headers[lower] = stored

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=request.app),
        base_url="http://replay",
    ) as client:
        resp = await client.post(
            "/v1/messages",
            content=body_bytes,
            headers=replay_headers,
        )

    return JSONResponse(
        {
            "replay_status": resp.status_code,
            "replay_body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
            "original_request_id": original.id,
        },
        status_code=resp.status_code,
    )


@router.get("/partials/diff")
async def partials_diff(
    request: Request,
    a: str = Query(..., description="First request ID"),
    b: str = Query(..., description="Second request ID"),
) -> HTMLResponse:
    """Rendered diff partial for htmx."""
    from fastapi.responses import JSONResponse as _  # noqa: F401

    storage = request.app.state.storage
    req_a = await storage.get_request_by_id(a) or await storage.get_request_by_short_id(a)
    req_b = await storage.get_request_by_id(b) or await storage.get_request_by_short_id(b)

    if req_a is None:
        return _render(request, "dashboard/diff_view.html", {"error": f"Request {a!r} not found"})
    if req_b is None:
        return _render(request, "dashboard/diff_view.html", {"error": f"Request {b!r} not found"})

    diff = _build_diff(req_a, req_b)
    return _render(request, "dashboard/diff_view.html", diff)


@router.get("/api/diff")
async def diff_requests(
    request: Request,
    a: str = Query(..., description="First request ID"),
    b: str = Query(..., description="Second request ID"),
):
    """Compare two captured requests side-by-side.

    Returns a structured diff of their bodies broken into logical sections:
    metadata (model, provider, timestamps), system prompt, messages, and
    parameters. Each section shows left/right values and whether they differ.
    """
    from fastapi.responses import JSONResponse

    storage = request.app.state.storage

    req_a = await storage.get_request_by_id(a) or await storage.get_request_by_short_id(a)
    req_b = await storage.get_request_by_id(b) or await storage.get_request_by_short_id(b)

    if req_a is None:
        return JSONResponse({"error": f"request {a!r} not found"}, status_code=404)
    if req_b is None:
        return JSONResponse({"error": f"request {b!r} not found"}, status_code=404)

    diff = _build_diff(req_a, req_b)
    return JSONResponse(diff)


def _build_diff(a, b) -> dict[str, Any]:
    """Build a structured diff between two RequestLog instances."""
    body_a = a.body or {}
    body_b = b.body or {}

    known_keys = {"model", "system", "messages", "max_tokens", "temperature",
                  "top_p", "top_k", "stop_sequences", "stream", "tools", "tool_choice"}

    metadata = {
        "timestamp": _diff_pair(a.timestamp.isoformat(), b.timestamp.isoformat()),
        "original_model": _diff_pair(a.original_model, b.original_model),
        "routed_model": _diff_pair(a.routed_model, b.routed_model),
        "routed_provider": _diff_pair(a.routed_provider, b.routed_provider),
        "status_code": _diff_pair(a.status_code, b.status_code),
        "input_tokens": _diff_pair(a.input_tokens, b.input_tokens),
        "output_tokens": _diff_pair(a.output_tokens, b.output_tokens),
    }

    system_a = body_a.get("system")
    system_b = body_b.get("system")

    messages_a = body_a.get("messages", [])
    messages_b = body_b.get("messages", [])

    params_a = {k: v for k, v in body_a.items() if k in known_keys and k not in ("model", "system", "messages")}
    params_b = {k: v for k, v in body_b.items() if k in known_keys and k not in ("model", "system", "messages")}

    extras_a = {k: v for k, v in body_a.items() if k not in known_keys}
    extras_b = {k: v for k, v in body_b.items() if k not in known_keys}

    return {
        "a_id": a.id,
        "b_id": b.id,
        "metadata": metadata,
        "system": _diff_pair(system_a, system_b),
        "messages": _diff_messages(messages_a, messages_b),
        "parameters": _diff_params(params_a, params_b),
        "extras": _diff_params(extras_a, extras_b),
    }


def _diff_pair(left, right) -> dict[str, Any]:
    return {"left": left, "right": right, "changed": left != right}


def _diff_messages(left: list, right: list) -> dict[str, Any]:
    """Compare message arrays with per-message alignment."""
    max_len = max(len(left), len(right))
    pairs = []
    for i in range(max_len):
        msg_a = left[i] if i < len(left) else None
        msg_b = right[i] if i < len(right) else None
        pairs.append({
            "index": i,
            "left": msg_a,
            "right": msg_b,
            "changed": msg_a != msg_b,
        })
    return {
        "left_count": len(left),
        "right_count": len(right),
        "pairs": pairs,
        "any_changed": any(p["changed"] for p in pairs),
    }


def _diff_params(left: dict, right: dict) -> dict[str, Any]:
    """Compare parameter dicts key-by-key."""
    all_keys = sorted(set(left) | set(right))
    fields = {}
    for key in all_keys:
        l_val = left.get(key)
        r_val = right.get(key)
        fields[key] = {"left": l_val, "right": r_val, "changed": l_val != r_val}
    return {"fields": fields, "any_changed": any(f["changed"] for f in fields.values())}
