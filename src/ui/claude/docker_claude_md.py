"""Sync the global ``~/.claude/CLAUDE.md`` into per-profile ``CLAUDE.md``s.

The user's global ``~/.claude/CLAUDE.md`` (managed by Claude > Settings)
holds rules that apply to every Claude Code session. When a profile
launches — whether via local PTY or Docker — its scoped
``CLAUDE.md`` (under ``profiles/<slug>/.claude/``) is materialised by
prepending the global content with a marker so the user can tell the
two sections apart on disk.

The profile-specific section can be edited via the profile editor's
existing ``claude_settings`` flow (the user has full control over
whatever they put in their own ``CLAUDE.md`` — this module just
guarantees the global content always leads).

Reads from ``~/.claude/CLAUDE.md`` are tolerant of a missing file —
``read_global_claude_md()`` returns the empty string in that case,
and the sync still produces a profile CLAUDE.md with just the
profile's own content.
"""

from __future__ import annotations

from pathlib import Path

from src.profiles.global_settings import GLOBAL_CLAUDE_MD_PATH
from src.profiles.models import ClaudeProfile
from src.profiles.paths_helpers import profile_config_base_dir


# Marker comment delimiting the global section in a profile's
# scoped ``CLAUDE.md``. ``#`` is a YAML/header comment, harmless in
# Markdown bodies; Claude Code treats it as text.
_GLOBAL_MARKER_PREFIX = (
    "# === Global CLAUDE.md (auto-prepended from "
)
_GLOBAL_MARKER_SUFFIX = ") ==="


def read_global_claude_md() -> str:
    """Return the user's global ``~/.claude/CLAUDE.md`` contents.

    Missing file → ``""`` (the caller's sync still produces a valid
    profile CLAUDE.md with just the profile's own content).
    """
    try:
        return GLOBAL_CLAUDE_MD_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def profile_claude_md_path(profile: ClaudeProfile) -> Path:
    """Absolute path to the profile's scoped ``CLAUDE.md``."""
    return profile_config_base_dir() / profile.slug / ".claude" / "CLAUDE.md"


def _format_global_section(global_text: str) -> str:
    """Wrap the global content with the marker comments.

    When the global file is empty, returns ``""`` — the profile's
    own ``CLAUDE.md`` shouldn't carry an empty marker block.
    """
    if not global_text.strip():
        return ""
    body = global_text.rstrip("\n")
    return (
        f"{_GLOBAL_MARKER_PREFIX}{GLOBAL_CLAUDE_MD_PATH}"
        f"{_GLOBAL_MARKER_SUFFIX}\n"
        f"{body}\n\n"
        f"# === End global section ===\n\n"
    )


def sync_profile_claude_md(profile: ClaudeProfile) -> Path:
    """Materialise the profile's scoped ``CLAUDE.md``.

    Writes to ``<data_dir>/profiles/<slug>/.claude/CLAUDE.md`` with
    the global content (if any) prepended. The profile's own body
    follows the global marker. The profile-side ``CLAUDE.md`` is
    empty by default — the proxy doesn't generate any per-profile
    content here; future iterations could append a profile-specific
    block derived from the editor's settings.

    Atomic write (write-to-temp + ``os.replace``) so a crash
    mid-write doesn't truncate the existing file.

    Returns the resolved path.
    """
    path = profile_claude_md_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    global_text = read_global_claude_md()
    body = _format_global_section(global_text)
    final = body  # profile body block is empty for now; reserved
    import os
    import tempfile

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        suffix=".tmp",
        prefix="CLAUDE_",
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(final)
            if not final.endswith("\n"):
                fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def sync_all_profile_claude_mds(profiles) -> list[Path]:
    """Sync every profile's scoped CLAUDE.md.

    Called from the global CLAUDE.md editor's Save handler so the
    user's edit propagates immediately to every running profile.
    Idempotent — repeated calls produce the same result.
    """
    return [sync_profile_claude_md(p) for p in profiles]