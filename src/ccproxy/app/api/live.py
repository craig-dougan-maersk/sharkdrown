"""Server-Sent Events endpoint for live request updates."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE stream of ``new_request`` events whenever a captured request lands.

    The dashboard's ``live.js`` opens an ``EventSource`` against this
    endpoint and refreshes the list on each event.
    """
    bus = request.app.state.event_bus

    async def event_gen() -> AsyncIterator[bytes]:
        # Initial ping so the EventSource connection is considered open
        # by the browser immediately (avoids a 30s+ wait on first event).
        yield b": ping\n\n"
        async for event in bus.subscribe():
            yield (f"event: new_request\ndata: {json.dumps(event)}\n\n").encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
