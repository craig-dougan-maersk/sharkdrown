"""Request-id generator."""

from __future__ import annotations

import secrets


def generate_request_id() -> str:
    """Return a 16-character hex id (8 random bytes).

    Used as the primary key for the captured-requests table. 16 hex chars
    matches the plan's schema and the upstream Go proxy's row-count
    behavior (8 bytes of entropy ≈ 2^64 keys before birthday collisions).
    """
    return secrets.token_hex(8)
