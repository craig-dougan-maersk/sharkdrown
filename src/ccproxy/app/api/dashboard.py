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
) -> HTMLResponse:
    """List fragment for the Conversations view."""
    base_path = _conversations_base(request)
    reader = ConversationReader(base_path=base_path)
    rows, total = reader.list_conversations(page=page, limit=limit)
    has_more = page * limit < total
    return _render(
        request,
        "dashboard/conversation_list.html",
        {
            "rows": rows,
            "has_more": has_more,
            "next_page": page + 1,
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


def _conversations_base(request: Request) -> Path | None:
    """Override hook for tests."""
    override = getattr(request.app.state, "conversations_base_path", None)
    return Path(override) if override is not None else None
