"""Regression test for the data_dir-not-threaded bug.

The user reported ``CLAUDE_CODE_NO_FLICKER`` and
``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`` were set in
``Claude > Settings > Env Vars`` but did NOT reach the Claude Code
container session. Root cause: ``claude_container_view.py`` called
``build_claude_session_env`` without passing ``data_dir``, so the
env-builder's global-env layer (PR4) was skipped entirely.

This test verifies the call site threads ``data_dir()`` through to
``build_claude_session_env``. We assert on the call signature rather
than the rendered env (which would require a full container launch),
so the test stays fast and doesn't need a real PTY/container.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def test_claude_container_view_passes_data_dir_to_env_builder() -> None:
    """``claude_container_view._build_claude_env_for_session`` (or
    whichever private method builds the session env) must pass
    ``data_dir`` to ``build_claude_session_env``.

    Inspect the source for the keyword-argument call. We use AST-style
    text matching rather than running the call (which would need a
    live proxy / PTY) so the test runs in milliseconds.
    """
    from src.ui.claude import claude_container_view

    source = inspect.getsource(claude_container_view)
    # The keyword must appear in a ``build_claude_session_env(...)``
    # call. Find the call and check the surrounding ~600 chars (the
    # function is large; the call is in the middle).
    idx = source.find("build_claude_session_env(")
    assert idx >= 0, "build_claude_session_env call not found"
    # Look ahead for ``data_dir=`` within the call's kwargs.
    snippet = source[idx : idx + 1500]
    assert "data_dir=" in snippet, (
        "build_claude_session_env call in claude_container_view does "
        "not pass data_dir — global env vars from Claude > Settings > "
        "Env Vars won't reach the container session. Regression: "
        "user reported CLAUDE_CODE_NO_FLICKER and "
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY were set in "
        "defaults.env but absent from the container env."
    )


def test_claude_container_view_passes_container_aware() -> None:
    """Sanity: the same call site still passes ``container_aware`` —
    ensures the data_dir addition didn't accidentally drop the
    container-rewrites."""
    from src.ui.claude import claude_container_view

    source = inspect.getsource(claude_container_view)
    idx = source.find("build_claude_session_env(")
    snippet = source[idx : idx + 1500]
    assert "container_aware=" in snippet
    assert "data_dir=" in snippet