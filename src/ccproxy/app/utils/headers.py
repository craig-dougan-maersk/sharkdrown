"""Header sanitization for captured-request persistence.

Sensitive headers are SHA-256 hashed so two captured requests with the same
``x-api-key`` value are comparable (via digests) without leaking the
plaintext key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

# Header-name substrings that mark a header as sensitive. Matched
# case-insensitively against the lower-cased key.
SENSITIVE_HEADER_TOKENS: tuple[str, ...] = (
    "x-api-key",
    "api-key",
    "authorization",
    "anthropic-api-key",
    "openai-api-key",
    "bearer",
    "cookie",
    "x-auth-token",
)


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with sensitive values replaced by ``sha256:<hex>``.

    Header names are preserved verbatim so a downstream debug view can still
    see "x-api-key: sha256:…". Sensitive-token matching is substring-based
    on the lower-cased name (matches the upstream Go behaviour).
    """
    out: dict[str, str] = {}
    for key, value in headers.items():
        lname = key.lower()
        if any(token in lname for token in SENSITIVE_HEADER_TOKENS):
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            out[key] = f"sha256:{digest}"
        else:
            out[key] = value
    return out
