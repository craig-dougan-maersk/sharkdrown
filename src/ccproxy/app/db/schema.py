"""SQLite schema for the captured-requests table.

The schema is intentionally minimal: a single ``requests`` table with
columns the dashboard reads and indexes the dashboard filters on. JSON
columns (headers, body, response, streaming_chunks, prompt_grade) hold
structured payloads that the proxy writes and reads verbatim.

Schema is versioned via the ``schema_version`` table. Migrations are
applied in :func:`_migrate` and are idempotent so the function is safe
to call on every startup.
"""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger("claude_code_proxy.db.schema")

SCHEMA_VERSION = 2

# Single source of truth. ``idempotent=True`` because every CREATE uses
# ``IF NOT EXISTS``; running the script multiple times is a no-op.
SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS requests (
        id                  TEXT PRIMARY KEY,
        timestamp           TEXT NOT NULL DEFAULT (datetime('now')),
        method              TEXT NOT NULL,
        endpoint            TEXT NOT NULL,
        status_code         INTEGER,
        request_path        TEXT NOT NULL DEFAULT 'passthrough',
        user_agent          TEXT,
        content_type        TEXT,
        original_model      TEXT,
        routed_model        TEXT,
        routed_provider     TEXT,
        headers             TEXT NOT NULL,
        body                TEXT NOT NULL,
        response            TEXT,
        streaming_chunks    TEXT,
        is_streaming        INTEGER NOT NULL DEFAULT 0,
        response_time_ms    INTEGER,
        completed_at        TEXT,
        prompt_grade        TEXT,
        input_tokens        INTEGER,
        output_tokens       INTEGER,
        total_tokens        INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_requests_endpoint   ON requests(endpoint)",
    "CREATE INDEX IF NOT EXISTS idx_requests_model     ON requests(original_model)",
    "CREATE INDEX IF NOT EXISTS idx_requests_provider  ON requests(routed_provider)",
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version    INTEGER NOT NULL PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
)


async def create_tables(conn: aiosqlite.Connection) -> None:
    """Apply the schema. Safe to call multiple times.

    Runs the base ``SCHEMA_SQL`` then any version-specific migrations
    needed to bring an existing database up to ``SCHEMA_VERSION``.
    """
    for stmt in SCHEMA_SQL:
        await conn.execute(stmt)
    await _migrate(conn)
    # Stamp schema_version with the current version if not already present.
    await conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    await conn.commit()
    logger.debug("schema applied (version=%d)", SCHEMA_VERSION)


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Apply incremental migrations to bring the DB up to ``SCHEMA_VERSION``.

    SQLite has no ``ADD COLUMN IF NOT EXISTS`` so each step inspects
    ``pragma_table_info`` first. New columns are intentionally nullable
    so old rows survive intact (their token counts just read as NULL
    until a request re-fills them).
    """
    # Migration v1 → v2: add token columns to ``requests``.
    cursor = await conn.execute("PRAGMA table_info(requests)")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if "input_tokens" not in existing:
        await conn.execute("ALTER TABLE requests ADD COLUMN input_tokens INTEGER")
    if "output_tokens" not in existing:
        await conn.execute("ALTER TABLE requests ADD COLUMN output_tokens INTEGER")
    if "total_tokens" not in existing:
        await conn.execute("ALTER TABLE requests ADD COLUMN total_tokens INTEGER")