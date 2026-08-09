"""Global Claude Code session environment variables.

The shipped defaults file (``defaults.env``) lives in the package as
``src/profiles/resources/defaults.env`` and is materialised to
``<data_dir>/defaults.env`` on first launch. The file is **editable**
by the user from ``Claude > Settings > Env Vars`` — ``save_global_env``
preserves the user's edits on subsequent calls (it only writes the
file when it already exists; ``ensure_global_env_file`` is the
idempotent first-launch deploy).

Layering order in :func:`build_claude_session_env` (``env_builder.py``):
    infrastructure → profile.env_vars → session_env → **global_env** →
    safety-net re-apply of infra vars.

The global layer is just above the safety-net so a user-edited
``defaults.env`` containing e.g. ``CLAUDE_CODE_NO_FLICKER=0`` wins
over the shipped default of ``1``, but the safety-net still
re-asserts ``ANTHROPIC_BASE_URL`` and friends — a ``defaults.env``
that tries to redirect proxy routing is a no-op.

File format
-----------
``KEY=VALUE`` lines, one per line. ``#`` introduces a comment to end
of line. Blank lines are skipped. ``export `` prefix is tolerated and
stripped. Values are kept verbatim (no shell expansion); they are
delivered to Claude Code as raw ``KEY=VALUE`` strings.

Why no hardcoded variables in ``env_builder.py``
-----------------------------------------------
Before this module, ``INFRA_ENV_VARS`` (a frozenset in
``env_builder.py``) named the integration-owned vars directly in
code. That frozenset stays — it represents the proxy's wire-protocol
contract (Anthropic's API expects ``ANTHROPIC_BASE_URL`` etc. to be
set), not user configuration. The user-configurable vars now live in
the deployed file.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


#: Filename for the deployed global env file. Sits next to
#: ``claude_global.json`` in the app data directory.
GLOBAL_ENV_FILENAME = "defaults.env"

#: Package resource path — must match the file at
#: ``src/profiles/resources/defaults.env``.
_PACKAGED_DEFAULTS_PATH = "defaults.env"

#: ``KEY=VALUE`` line. Captures ``KEY`` (group 1, no whitespace) and
#: the rest of the line as the raw value (group 2). We don't try to
#: parse quoted strings, escapes, or interpolation — the file is for
#: Claude Code env vars, which are short identifiers + numbers.
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def ensure_global_env_file(data_dir: Path) -> Path:
    """Deploy the packaged ``defaults.env`` to ``<data_dir>`` on first launch.

    Idempotent: if the file already exists (e.g. the user has edited it),
    this function is a no-op. Returns the path to the deployed file.

    The shipped template carries only the two new Claude Code env vars
    (``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`` and
    ``CLAUDE_CODE_NO_FLICKER``); the rest of the integration-owned
    infrastructure (``ANTHROPIC_BASE_URL`` etc.) stays in
    ``env_builder.INFRA_ENV_VARS`` as code.
    """
    target = Path(data_dir) / GLOBAL_ENV_FILENAME
    if target.exists():
        # User-edited file present — leave it alone. ``save_global_env``
        # owns subsequent writes.
        return target

    data_dir.mkdir(parents=True, exist_ok=True)

    template_bytes = _read_packaged_template()
    # Write atomically with restrictive permissions — even though the
    # shipped template has no secrets, the user can edit it freely to
    # add e.g. ``ANTHROPIC_API_KEY`` overrides, so chmod 600 from the
    # first write onwards.
    _atomic_write_text(target, template_bytes, mode=0o600)
    return target


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``defaults.env``-format file into a dict.

    Skips blank lines and ``#`` comments. ``export `` prefix is
    stripped. Values are returned verbatim (no shell expansion). The
    return type is a plain dict so callers can mutate it before
    passing to :func:`save_global_env`.
    """
    out: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(raw)
        if not m:
            # Tolerate malformed lines (don't crash the editor) but
            # log so the user can spot a typo.
            logger.warning(
                "Ignoring malformed line in %s: %r", path, raw
            )
            continue
        key, value = m.group(1), m.group(2)
        out[key] = value
    return out


def save_global_env(data_dir: Path, env: dict[str, str]) -> Path:
    """Write ``env`` back to ``<data_dir>/defaults.env``.

    Atomic write via ``tempfile.mkstemp`` + ``os.replace`` with
    ``chmod 600``. Preserves ``# comments`` and blank lines by
    serialising the dict to a canonical form (``# `` header + one
    ``KEY=VALUE`` per line).

    Returns the path to the written file.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    target = Path(data_dir) / GLOBAL_ENV_FILENAME
    rendered = _render_env(env)
    _atomic_write_text(target, rendered, mode=0o600)
    return target


def load_global_env(data_dir: Path) -> dict[str, str]:
    """Ensure the file exists, then parse and return the env dict.

    Combines :func:`ensure_global_env_file` and :func:`parse_env_file`
    for the common call-site in :func:`build_claude_session_env`.
    """
    path = ensure_global_env_file(Path(data_dir))
    return parse_env_file(path)


def _render_env(env: dict[str, str]) -> str:
    """Serialise ``env`` to the canonical file format."""
    lines = [
        "# SharkDrown global Claude Code session env vars.",
        "# Editable from Claude > Settings > Env Vars; chmod 600 on save.",
        "",
    ]
    for key in sorted(env):
        lines.append(f"{key}={env[key]}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _atomic_write_text(target: Path, content: str, mode: int) -> None:
    """Atomic text write with restrictive mode, mirroring
    ``GlobalSettingsStore._save_raw``."""
    fd, tmp = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        # ``fchmod`` BEFORE close so there's no window where the
        # temp file is open with default (umask-dependent) permissions.
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup; re-raise the original error.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_packaged_template() -> str:
    """Read the packaged ``defaults.env`` via ``importlib.resources``.

    Falls back to a hard-coded minimum content if the packaged
    resource is missing (e.g. a non-wheel install that didn't pick up
    package-data) so first-launch users still get sensible defaults
    rather than an empty file.
    """
    try:
        from importlib.resources import files

        resource = files("src.profiles.resources").joinpath(
            _PACKAGED_DEFAULTS_PATH
        )
        # ``read_text`` raises ``FileNotFoundError`` on Python 3.11
        # when the resource is missing — older versions returned
        # ``None`` or ``NotImplemented``. We catch the broad
        # ``OSError`` family to be portable across CPython versions.
        try:
            return resource.read_text(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass
    except (ModuleNotFoundError, ImportError):
        pass

    # Fallback: the canonical template as text. Matches the
    # packaged ``src/profiles/resources/defaults.env``.
    return (
        "# SharkDrown global Claude Code session env vars.\n"
        "# Editable from Claude > Settings > Env Vars; chmod 600 on save.\n"
        "\n"
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1\n"
        "CLAUDE_CODE_NO_FLICKER=1\n"
    )