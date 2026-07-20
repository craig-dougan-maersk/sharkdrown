"""Monitor endpoints — list/delete captured requests."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ccproxy.app.models.storage import RequestLog, ResponseLog

router = APIRouter(prefix="/api")

router = APIRouter(prefix="/api")


@router.get("/stats")
async def stats(request: Request) -> JSONResponse:
    """Aggregate counts for the desktop status-bar tile.

    Lightweight: three COUNT(*) queries on indexed columns plus one
    ``ORDER BY ... LIMIT 1`` for last-activity metadata. Designed to be
    polled every few seconds from the main Qt thread without
    noticeable latency. Response shape::

        {
            "total":    <int>,    # all rows
            "failed":   <int>,    # status >= 400
            "empty":    <int>,    # total_tokens IS NULL OR 0
            "last":     {         # most recent row, or null
                "timestamp": <iso8601 | null>,
                "model":     <str   | null>
            }
        }
    """
    storage = request.app.state.storage
    total = await storage.count_total()
    failed = await storage.count_failed()
    empty = await storage.count_no_tokens()
    last_ts, last_model = await storage.last_request_info()
    return JSONResponse(
        {
            "total": total,
            "failed": failed,
            "empty": empty,
            "last": {"timestamp": last_ts, "model": last_model},
        }
    )


@router.get("/requests")
async def list_requests(
    request: Request,
    page: int = 1,
    limit: int = 50,
    model: str | None = None,
    provider: str | None = None,
    hide_failed: str | None = None,
    hide_no_tokens: str | None = None,
) -> JSONResponse:
    """Paginated list of captured requests.

    Optional filters:
        model    — case-insensitive substring match on original_model
        provider — exact match on routed_provider (anthropic | openai)
        hide_failed     — drop status >= 400 (default ON)
        hide_no_tokens  — drop rows with NULL/zero total_tokens (default ON)

    The two ``hide_*`` params follow the same default-ON semantics
    as the dashboard's filter toggles: absent or empty resolves to
    the default, ``"0"`` flips it off, anything else keeps it on.
    """
    from ccproxy.app.api.dashboard import _FILTER_DEFAULTS, _filter_bool
    storage = request.app.state.storage
    rows, total = await storage.get_requests(
        page=page,
        limit=limit,
        model_filter=model if model and model != "all" else None,
        provider_filter=provider if provider and provider != "all" else None,
        hide_failed=_filter_bool("hide_failed", hide_failed),
        hide_no_tokens=_filter_bool("hide_no_tokens", hide_no_tokens),
    )
    return JSONResponse(
        {
            "requests": [_request_log_to_json(r) for r in rows],
            "total": total,
            "page": page,
            "limit": limit,
        }
    )


@router.delete("/requests")
async def clear_requests(request: Request) -> JSONResponse:
    """Delete every captured request."""
    storage = request.app.state.storage
    deleted = await storage.clear_requests()
    return JSONResponse({"message": "All requests cleared", "deleted": deleted})


def _request_log_to_json(r: RequestLog) -> dict[str, object]:
    """Serialize a RequestLog for JSON output (datetimes → ISO strings)."""
    return {
        "id": r.id,
        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        "method": r.method,
        "endpoint": r.endpoint,
        "status_code": r.status_code,
        "request_path": r.request_path,
        "user_agent": r.user_agent,
        "content_type": r.content_type,
        "original_model": r.original_model,
        "routed_model": r.routed_model,
        "routed_provider": r.routed_provider,
        "headers": r.headers,
        "body": r.body,
        "response": _response_log_to_json(r.response) if r.response else None,
        "prompt_grade": r.prompt_grade.model_dump(mode="json") if r.prompt_grade else None,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "total_tokens": r.total_tokens,
    }


def _response_log_to_json(r: ResponseLog) -> dict[str, object]:
    return {
        "status_code": r.status_code,
        "headers": r.headers,
        "body_text": r.body_text,
        "response_time_ms": r.response_time_ms,
        "is_streaming": r.is_streaming,
        "streaming_chunks": r.streaming_chunks,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }
