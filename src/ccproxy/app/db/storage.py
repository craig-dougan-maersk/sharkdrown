"""Async SQLite storage for captured proxy requests.

A fresh ``aiosqlite.Connection`` is opened per ``StorageService`` instance
because SQLite is single-file and async-sqlite serializes writes internally.
WAL mode is enabled so concurrent readers (e.g. the dashboard) don't block
the writer (the proxy).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from ccproxy.app.db.schema import create_tables
from ccproxy.app.models import PromptGrade, RequestLog, ResponseLog
from ccproxy.app.models.storage import RequestPath

logger = logging.getLogger("claude_code_proxy.db.storage")


class StorageService:
    """Async CRUD over the captured-requests table."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        event_bus: object | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        # Optional in-process pub/sub. Avoids a hard import to keep the
        # storage module decoupled from the live/ package.
        self._event_bus = event_bus

    async def connect(self) -> None:
        """Open the connection and ensure the schema exists."""
        if self._conn is not None:
            return
        try:
            self._conn = await aiosqlite.connect(self.db_path)
        except sqlite3.OperationalError as exc:
            # The generic SQLite error hides the actual OS error (usually
            # EACCES). On bind-mounted data dirs the host user owns the
            # directory and the container's appuser can't write to it.
            if "unable to open database file" in str(exc):
                parent = Path(self.db_path).parent
                raise RuntimeError(
                    f"Cannot open SQLite database at {self.db_path!r}. "
                    f"The parent directory {parent!s} likely does not exist "
                    f"or is not writable by the current user (UID {os.getuid()}). "
                    f"If you're running in Docker with a bind-mounted "
                    f"/app/data, ensure the host directory is writable by the "
                    f"container's user (UID 1001 by default): "
                    f"`mkdir -p -m 777 ./data` before `docker run`."
                ) from exc
            raise
        # WAL mode lets concurrent readers proceed alongside the writer.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await create_tables(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> StorageService:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("StorageService.connect() not called")
        return self._conn

    # ---- CRUD ----

    async def save_request(self, req: RequestLog) -> str:
        """Persist a request row (without response). Returns the id."""
        await self.conn.execute(
            """
            INSERT INTO requests (
                id, timestamp, method, endpoint,
                user_agent, content_type,
                original_model, routed_model, routed_provider,
                request_path, headers, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.id,
                req.timestamp.isoformat(),
                req.method,
                req.endpoint,
                req.user_agent,
                req.content_type,
                req.original_model,
                req.routed_model,
                req.routed_provider,
                req.request_path,
                _json(req.headers),
                _json(req.body),
            ),
        )
        await self.conn.commit()
        # Publish to the live-update bus (if attached).
        if self._event_bus is not None:
            publish = getattr(self._event_bus, "publish", None)
            if callable(publish):
                publish(
                    {
                        "id": req.id,
                        "endpoint": req.endpoint,
                        "timestamp": req.timestamp.isoformat() if req.timestamp else None,
                        "original_model": req.original_model,
                        "routed_model": req.routed_model,
                    }
                )
        return req.id

    async def get_requests(
        self,
        page: int = 1,
        limit: int = 50,
        *,
        model_filter: str | None = None,
        provider_filter: str | None = None,
        endpoint_filter: str | None = None,
        hide_failed: bool = False,
        hide_no_tokens: bool = False,
    ) -> tuple[list[RequestLog], int]:
        """Return ``(rows, total)`` paginated and filtered.

        ``hide_failed`` drops 4xx/5xx (and isolates failed-before-capture
        rows from the same surface). ``hide_no_tokens`` drops rows with no
        usage data — typically short-circuited or aborted calls that
        contributed nothing useful.
        """
        where, args = _build_where(
            model_filter=model_filter,
            provider_filter=provider_filter,
            endpoint_filter=endpoint_filter,
            hide_failed=hide_failed,
            hide_no_tokens=hide_no_tokens,
        )
        # Total count (before pagination).
        async with self.conn.execute(f"SELECT COUNT(*) FROM requests {where}", args) as cur:
            row = await cur.fetchone()
            total = int(row[0]) if row else 0

        # Page.
        page = max(1, page)
        limit = max(1, limit)
        offset = (page - 1) * limit
        sql = f"""
            SELECT
                id, timestamp, method, endpoint, status_code, request_path,
                user_agent, content_type, original_model, routed_model,
                routed_provider, headers, body, response, streaming_chunks,
                is_streaming, response_time_ms, completed_at, prompt_grade,
                input_tokens, output_tokens, total_tokens
            FROM requests
            {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        async with self.conn.execute(sql, [*args, limit, offset]) as cur:
            rows = await cur.fetchall()
        return [_row_to_request_log(r) for r in rows], total

    async def get_request_by_id(self, request_id: str) -> RequestLog | None:
        """Fetch a single request by exact id."""
        async with self.conn.execute(
            """
            SELECT
                id, timestamp, method, endpoint, status_code, request_path,
                user_agent, content_type, original_model, routed_model,
                routed_provider, headers, body, response, streaming_chunks,
                is_streaming, response_time_ms, completed_at, prompt_grade,
                input_tokens, output_tokens, total_tokens
            FROM requests
            WHERE id = ?
            """,
            (request_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_request_log(row) if row else None

    async def get_request_by_short_id(self, short_id: str) -> RequestLog | None:
        """Fetch by short id (LIKE prefix match)."""
        async with self.conn.execute(
            """
            SELECT
                id, timestamp, method, endpoint, status_code, request_path,
                user_agent, content_type, original_model, routed_model,
                routed_provider, headers, body, response, streaming_chunks,
                is_streaming, response_time_ms, completed_at, prompt_grade,
                input_tokens, output_tokens, total_tokens
            FROM requests
            WHERE id LIKE ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (f"{short_id}%",),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_request_log(row) if row else None

    async def update_request_with_response(self, request_id: str, response: ResponseLog) -> None:
        # Extract token counts from the response so the dashboard can show
        # them in the request list without re-parsing the body. Look in
        # the JSON body first (non-streaming); fall back to the
        # ``message_delta`` event in ``streaming_chunks`` (streaming),
        # which Anthropic sends just before ``message_stop`` and carries
        # the final ``usage`` block.
        input_tokens: int | None = None
        output_tokens: int | None = None
        if response.body_text:
            try:
                obj = json.loads(response.body_text)
            except (json.JSONDecodeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    input_tokens = _safe_int(usage.get("input_tokens"))
                    output_tokens = _safe_int(usage.get("output_tokens"))
        if (input_tokens is None or output_tokens is None) and response.streaming_chunks:
            for chunk in response.streaming_chunks:
                # Each chunk is an SSE record that may contain both
                # ``event: ...`` and ``data: ...`` lines. Find the JSON
                # payload on the ``data:`` line (if present).
                payload = None
                for line in chunk.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        payload = line[len("data:") :].lstrip()
                        break
                if payload is None:
                    # Fallback: chunk is just the JSON without an event
                    # line (older recordings).
                    if chunk.lstrip().startswith("{"):
                        payload = chunk
                if payload is None:
                    continue
                try:
                    obj = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                # Anthropic streaming: ``message_delta`` carries usage.
                if obj.get("type") == "message_delta":
                    usage = obj.get("usage") or {}
                    if isinstance(usage, dict):
                        if input_tokens is None:
                            input_tokens = _safe_int(usage.get("input_tokens"))
                        if output_tokens is None:
                            output_tokens = _safe_int(usage.get("output_tokens"))
                # Anthropic message_start also carries input_tokens.
                if obj.get("type") == "message_start" and input_tokens is None:
                    usage = (obj.get("message") or {}).get("usage") or {}
                    if isinstance(usage, dict):
                        input_tokens = _safe_int(usage.get("input_tokens"))
        total_tokens: int | None = None
        if input_tokens is not None or output_tokens is not None:
            total_tokens = (input_tokens or 0) + (output_tokens or 0)

        await self.conn.execute(
            """
            UPDATE requests
            SET status_code = ?,
                response = ?,
                streaming_chunks = ?,
                is_streaming = ?,
                response_time_ms = ?,
                completed_at = ?,
                input_tokens = ?,
                output_tokens = ?,
                total_tokens = ?
            WHERE id = ?
            """,
            (
                response.status_code,
                _json(
                    {
                        "headers": response.headers,
                        "body_text": response.body_text,
                    }
                ),
                _json(response.streaming_chunks),
                1 if response.is_streaming else 0,
                response.response_time_ms,
                response.completed_at.isoformat() if response.completed_at else None,
                input_tokens,
                output_tokens,
                total_tokens,
                request_id,
            ),
        )
        await self.conn.commit()

    async def update_request_with_grading(self, request_id: str, grade: PromptGrade) -> None:
        await self.conn.execute(
            "UPDATE requests SET prompt_grade = ? WHERE id = ?",
            (_json(grade.model_dump(mode="json")), request_id),
        )
        await self.conn.commit()

    async def clear_requests(self) -> int:
        async with self.conn.execute("DELETE FROM requests") as cur:
            await cur.fetchone()
        await self.conn.commit()
        # aiosqlite returns rowcount via cursor; use that for the count.
        return cur.rowcount if cur.rowcount is not None else 0

    # ---- Aggregate counters (used by /api/stats) ----

    async def count_total(self) -> int:
        """Total captured-request count, ignoring pagination."""
        async with self.conn.execute("SELECT COUNT(*) FROM requests") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_failed(self) -> int:
        """Count of requests that returned HTTP 4xx or 5xx.

        In-flight rows (``status_code IS NULL``) are *not* counted as
        failures — those are still being captured and may yet succeed.
        """
        async with self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status_code >= 400"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_no_tokens(self) -> int:
        """Count of requests with no usable token accounting.

        Returns rows where ``total_tokens`` is NULL or zero — these are
        the proxy's analogue of Claude Code's empty subagent entries: a
        request that completed but contributed nothing to the conversation.
        """
        async with self.conn.execute(
            "SELECT COUNT(*) FROM requests "
            "WHERE total_tokens IS NULL OR total_tokens = 0"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def last_request_info(self) -> tuple[str | None, str | None]:
        """Return ``(iso_timestamp, model)`` for the most recent request.

        Both fields are ``None`` when the table is empty. Used by the
        status-bar tile to show "last activity at HH:MM via claude-...".
        """
        async with self.conn.execute(
            "SELECT timestamp, original_model FROM requests "
            "ORDER BY timestamp DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None, None
        return row[0], row[1]


# ---- Helpers ----------------------------------------------------------------


def _json(obj: Any) -> str:
    """Serialize ``obj`` to JSON. Pydantic models are dumped via ``mode='json'``."""
    if obj is None:
        return "null"
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, default=str, ensure_ascii=False)


def _build_where(
    *,
    model_filter: str | None,
    provider_filter: str | None,
    endpoint_filter: str | None,
    hide_failed: bool = False,
    hide_no_tokens: bool = False,
) -> tuple[str, tuple[Any, ...]]:
    """Compose the WHERE clause + bound args from filter flags.

    All clauses are ANDed. ``hide_failed`` keeps NULL status (in-flight
    rows) alongside 1xx/2xx/3xx so a request that's mid-capture still
    appears; only real errors (4xx/5xx) are dropped.
    """
    clauses: list[str] = []
    args: list[Any] = []
    if model_filter:
        clauses.append("LOWER(original_model) LIKE ?")
        args.append(f"%{model_filter.lower()}%")
    if provider_filter:
        clauses.append("routed_provider = ?")
        args.append(provider_filter)
    if endpoint_filter:
        clauses.append("endpoint = ?")
        args.append(endpoint_filter)
    if hide_failed:
        clauses.append("(status_code IS NULL OR status_code < 400)")
    if hide_no_tokens:
        # NULL or zero means no token info — aborted requests, errored
        # before send, or old rows pre-token-tracking.
        clauses.append("(total_tokens IS NOT NULL AND total_tokens > 0)")
    if not clauses:
        return "", ()
    return "WHERE " + " AND ".join(clauses), tuple(args)


def _row_to_request_log(row: Any) -> RequestLog:
    """Convert a SQLite row (aiosqlite.Row, supports [] and tuple unpacking) to RequestLog."""
    (
        rid,
        timestamp,
        method,
        endpoint,
        status_code,
        request_path,
        user_agent,
        content_type,
        original_model,
        routed_model,
        routed_provider,
        headers_json,
        body_json,
        response_json,
        streaming_chunks_json,
        is_streaming,
        response_time_ms,
        completed_at,
        prompt_grade_json,
        input_tokens,
        output_tokens,
        total_tokens,
    ) = row

    headers = json.loads(headers_json) if headers_json else {}
    body = json.loads(body_json) if body_json else {}
    response = None
    if response_json:
        response_dict = json.loads(response_json)
        response = ResponseLog(
            status_code=status_code if status_code is not None else 0,
            headers=response_dict.get("headers", {}),
            body_text=response_dict.get("body_text"),
            response_time_ms=response_time_ms,
            is_streaming=bool(is_streaming),
            streaming_chunks=json.loads(streaming_chunks_json) if streaming_chunks_json else [],
            completed_at=_parse_dt(completed_at),
        )
    prompt_grade = None
    if prompt_grade_json:
        prompt_grade = PromptGrade(**json.loads(prompt_grade_json))

    return RequestLog(
        id=rid,
        timestamp=_parse_dt(timestamp),
        method=method,
        endpoint=endpoint,
        status_code=status_code,
        request_path=cast(RequestPath, request_path or "passthrough"),
        user_agent=user_agent,
        content_type=content_type,
        original_model=original_model,
        routed_model=routed_model,
        routed_provider=routed_provider,
        headers=headers,
        body=body,
        response=response,
        prompt_grade=prompt_grade,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _safe_int(value: Any) -> int | None:
    """Coerce a JSON-decoded value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Any:
    """Parse a SQLite datetime string into a ``datetime``."""
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    # SQLite stores ISO strings; handle the common forms.
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return value


# Cast helper (kept local to avoid importing typing.cast elsewhere).
def cast(t: Any, v: Any) -> Any:
    return v


# ---- Factory used by FastAPI dependencies (Phase 6) -------------------------


_storage_singleton: StorageService | None = None


async def get_storage_service() -> AsyncIterator[StorageService]:
    """FastAPI dependency yielding a StorageService.

    For Phase 3 we open one per request; Phase 6 will swap this for an
    ``app.state``-backed instance so the connection lives for the process.
    """
    svc = StorageService("requests.db")
    await svc.connect()
    try:
        yield svc
    finally:
        await svc.close()


@asynccontextmanager
async def open_storage(db_path: str | Path) -> AsyncIterator[StorageService]:
    """Async context manager for tests and scripts."""
    svc = StorageService(db_path)
    await svc.connect()
    try:
        yield svc
    finally:
        await svc.close()
