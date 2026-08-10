"""Regression: the container image's Claude Code CLI must be
2.1.223+ for /v1/models discovery to work.

Background
----------
Claude Code's /v1/models discovery filter changed between
versions:

  - Pre-2.1.223: ``id`` must BEGIN WITH ``claude`` or
    ``anthropic`` (case-insensitive). Synthetic ids that contain
    but don't start with those substrings (e.g.
    ``minimax-non-anthropic``) are silently dropped from the
    picker.

  - 2.1.223+: filter relaxed to "id CONTAINS ``claude`` or
    ``anthropic``" — the synthetic-id scheme used by
    ``src/profiles/discovery_id.py`` works as designed.

Our picker relies on the relaxed filter. Pinning the Dockerfile
below 2.1.223 leaves the picker empty for non-claude aliases
even though everything else works. This test catches that
regression.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Add tests/ to sys.path so the test can be discovered without
# requiring the project to be installed.
sys.path.insert(0, str(Path(__file__).parent))


def test_dockerfile_claude_code_version_at_least_2_1_223() -> None:
    """The Dockerfile's default ``CLAUDE_CODE_VERSION`` must be
    2.1.223 or newer so /v1/models discovery accepts synthetic
    ids with the ``-non-anthropic`` suffix.

    Skips the check if the default is ``latest`` (which we'd
    expect to be ≥ 2.1.223 in practice but can't verify
    statically)."""
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile.claude-code"
    text = dockerfile.read_text(encoding="utf-8")

    # Find the ARG CLAUDE_CODE_VERSION=... line.
    match = re.search(
        r"^ARG\s+CLAUDE_CODE_VERSION\s*=\s*(\S+)\s*$",
        text,
        re.MULTILINE,
    )
    assert match is not None, (
        f"could not find ARG CLAUDE_CODE_VERSION line in {dockerfile}"
    )
    version = match.group(1)

    # ``latest`` is fine — we just can't verify statically. The
    # test guards against hardcoded stale versions (e.g. pinning
    # ``2.1.126`` because that's the first version that added
    # discovery at all, before the filter was relaxed).
    if version == "latest":
        return

    # Parse ``X.Y.Z`` or ``X.Y``. We only need major + minor.
    parts = version.split(".")
    assert len(parts) >= 2, f"unexpected version format: {version!r}"
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except ValueError:
        # Could be ``2.1.223-rc1`` — skip.
        return

    assert (major, minor) >= (2, 1), (
        f"Dockerfile pins Claude Code {version!r}; major.minor must "
        f"be ≥ 2.1 (any 2.x line is fine; the filter-relaxation "
        f"is at 2.1.223)"
    )

    # If we're explicitly pinning a 2.1.x version (not 2.2+), it
    # must be ≥ 2.1.223 specifically.
    if major == 2 and minor == 1:
        # Strip any pre-release suffix from the patch part.
        patch_str = re.match(r"\d+", parts[2] if len(parts) >= 3 else "0")
        assert patch_str is not None
        patch = int(patch_str.group())
        assert patch >= 223, (
            f"Dockerfile pins Claude Code {version!r}; 2.1.x "
            f"versions < 2.1.223 have the strict discovery filter "
            f"(id must BEGIN with claude/anthropic), which rejects "
            f"our synthetic ids. Bump to 2.1.223+ or to a newer "
            f"2.x line."
        )