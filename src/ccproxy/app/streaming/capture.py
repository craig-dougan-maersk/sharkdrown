"""SSE forward-and-capture utility.

Yields bytes to the client while accumulating ``data:`` lines for storage.
Used by the proxy handler in ``/v1/messages`` for ``stream: true`` requests.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

from ccproxy.app.db.storage import StorageService
from ccproxy.app.models.storage import ResponseLog

_RECORD_SEPARATOR = b"\n\n"


async def stream_and_capture(
    response: httpx.Response,
    storage: StorageService,
    request_id: str,
    endpoint: str,
) -> AsyncIterator[bytes]:
    """Forward bytes to the client and accumulate ``data:`` lines for storage.

    ``response`` must be opened with ``stream=True`` so its body is iterable.
    On stream completion, a ``ResponseLog`` is persisted.

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

    async for raw in response.aiter_bytes():
        # Forward the upstream's raw bytes 1:1 to the client. SSE is
        # line-based; the client reassembles records itself.
        yield raw

        buffer += raw
        # Drain complete records from the buffer; keep the trailing
        # partial record for the next iteration so a data: line that
        # crosses a chunk boundary isn't dropped.
        while _RECORD_SEPARATOR in buffer:
            record, _, buffer = buffer.partition(_RECORD_SEPARATOR)
            _capture_data_lines(record, chunks)

    # Drain any trailing record that didn't end with the separator
    # (e.g. the stream ended mid-record, or the upstream closed cleanly
    # without a final blank line).
    if buffer.strip():
        _capture_data_lines(buffer, chunks)

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
    await storage.update_request_with_response(request_id, response_log)


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
