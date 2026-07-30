"""SSE forward-and-capture utility.

Yields bytes to the client while accumulating ``data:`` lines for storage.
Used by the proxy handler in ``/v1/messages`` for ``stream: true`` requests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from ccproxy.app.db.storage import StorageService
from ccproxy.app.models.storage import ResponseLog

_RECORD_SEPARATOR = b"\n\n"
# Cap on SSE chunks retained for dashboard storage. Client forwarding is
# unaffected — all bytes are still yielded; only the in-memory capture
# list is bounded. 2000 chunks covers the largest practical responses.
_MAX_CAPTURE_CHUNKS = 2000

logger = logging.getLogger("claude_code_proxy.streaming.capture")


async def stream_and_capture(
    response: httpx.Response,
    storage: StorageService,
    request_id: str,
    endpoint: str,
) -> AsyncIterator[bytes]:
    """Forward bytes to the client and accumulate ``data:`` lines for storage.

    ``response`` must be opened with ``stream=True`` so its body is iterable.
    On stream completion (or client disconnect), a ``ResponseLog`` is persisted
    via a ``finally`` block. Python calls ``.aclose()`` on an async generator
    when its consumer stops iterating (e.g. client disconnect), and ``.aclose()``
    properly executes ``finally`` blocks including their ``await`` expressions,
    so the DB write happens regardless of how the stream ends.

    The capture is byte-aware: ``response.aiter_bytes()`` may deliver
    chunks whose boundaries cut a single ``data:`` JSON line in two. We
    keep a rolling buffer of incomplete bytes between calls so the
    capture never loses the tail half of a split line. The boundary
    ``\\n\\n`` (SSE's record separator) is the marker that delimits a
    complete record — anything before the next ``\\n\\n`` is held for the
    next iteration.
    """
    chunks: list[str] = []
    start = time.monotonic()
    status_code = response.status_code
    buffer = b""
    capture_full = True  # False once the cap is reached

    try:
        async for raw in response.aiter_bytes():
            # Forward the upstream's raw bytes 1:1 to the client. SSE is
            # line-based; the client reassembles records itself.
            yield raw

            if capture_full:
                buffer += raw
                # Drain complete records from the buffer; keep the trailing
                # partial record for the next iteration so a data: line that
                # crosses a chunk boundary isn't dropped.
                while _RECORD_SEPARATOR in buffer:
                    record, _, buffer = buffer.partition(_RECORD_SEPARATOR)
                    _capture_data_lines(record, chunks)
                    if len(chunks) >= _MAX_CAPTURE_CHUNKS:
                        logger.debug(
                            "stream capture cap reached (%d chunks) for request %s; "
                            "further chunks will not be stored",
                            _MAX_CAPTURE_CHUNKS,
                            request_id,
                        )
                        capture_full = False
                        buffer = b""
                        break

        # Drain any trailing record that didn't end with the separator
        # (e.g. the stream ended mid-record, or the upstream closed cleanly
        # without a final blank line).
        if capture_full and buffer.strip():
            _capture_data_lines(buffer, chunks)

    finally:
        # Always persist whatever was captured — handles both clean
        # completion and early termination (client disconnect, upstream
        # error). The elapsed time reflects wall-clock up to this point.
        elapsed_ms = int((time.monotonic() - start) * 1000)
        response_log = ResponseLog(
            status_code=status_code,
            headers=dict(response.headers.items()),
            is_streaming=True,
            streaming_chunks=chunks,
            response_time_ms=elapsed_ms,
            completed_at=datetime.now(UTC),
            body_text="\n\n".join(chunks),
        )
        try:
            await storage.update_request_with_response(request_id, response_log)
        except Exception as exc:
            logger.warning(
                "failed to persist response log for request %s: %s",
                request_id,
                exc,
            )


def _capture_data_lines(record: bytes, chunks: list[str]) -> None:
    """Pull the ``data: ...`` lines out of one SSE record (already split
    off the byte stream) and append them to ``chunks``. The ``[DONE]``
    sentinel is filtered because Claude Code's client doesn't want it
    re-emitted on a retry of a captured event."""
    for line in record.splitlines():
        try:
            text = line.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            continue
        if not text.startswith("data:"):
            continue
        if text.startswith("data: [DONE]"):
            continue
        chunks.append(text)
