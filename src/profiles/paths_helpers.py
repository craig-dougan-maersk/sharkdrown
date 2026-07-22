"""Path helpers shared between profile storage and the Claude UI.

Kept separate from :mod:`src.profiles.store` so the UI layer doesn't
pull in :class:`ProfileStore` just to learn where profile config dirs
live, and so the helper can be unit-tested without spinning up a
``QApplication`` (which :func:`data_dir` requires).
"""

from __future__ import annotations

from pathlib import Path

from src.proxy.paths import data_dir


def profile_config_base_dir() -> Path:
    """Root directory under which per-profile ``CLAUDE_CONFIG_DIR``s live.

    Returns ``<app_data>/claude_profiles``. The parent is created
    on demand by callers (:func:`src.profiles.config_dir.write_profile_config`)
    so this helper stays read-only.

    NB: requires ``QStandardPaths.writableLocation`` to resolve —
    that's only valid after a ``QApplication`` has been constructed.
    Tests that hit this path either run inside ``test_claude.py``
    (which calls ``_qapp()`` first) or stub ``src.proxy.paths.data_dir``.
    """
    return data_dir() / "claude_profiles"