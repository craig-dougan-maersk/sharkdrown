"""Data model for Claude Code session profiles.

Profiles are persisted as JSON by :class:`src.profiles.store.ProfileStore`
and on disk each profile also gets its own ``CLAUDE_CONFIG_DIR`` directory
(``<data_dir>/claude_profiles/<slug>/``) holding ``settings.json`` and
``agents/*.md`` — written by :func:`src.profiles.config_dir.write_profile_config`.

The dataclasses here are deliberately *not* ``frozen``: profile editing is a
copy-and-commit pattern at the UI layer (the dialog edits a fresh profile
and only writes on OK), but :func:`dataclasses.replace` is the canonical
mutation path, and a non-frozen shape keeps that simple. The store reads
into fresh instances and writes dict-shaped JSON; nothing else mutates a
profile in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---- slug -----------------------------------------------------------------

_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_DASH_RE = re.compile(r"-+")


def slugify(name: str) -> str:
    """Return a filesystem-safe slug derived from ``name``.

    Lowercases, replaces runs of non-``[a-z0-9-]`` with ``-``, and
    collapses multiple dashes. Empty / pure-punctuation inputs fall
    back to ``"profile"`` so the resulting path is never empty.

    Used for both the on-disk ``claude_profiles/<slug>/`` directory
    and the credentials file key (``default-key`` etc.) — both must
    be stable across renames of the profile's human-readable name so
    we don't accidentally leak credentials under a new directory after
    a rename. The slug is also used in tab labels so collisions across
    profiles get a numeric suffix in :class:`ProfileStore`.
    """
    s = name.strip().lower()
    s = _SLUG_STRIP_RE.sub("-", s)
    s = _SLUG_DASH_RE.sub("-", s).strip("-")
    return s or "profile"


# ---- dataclasses ----------------------------------------------------------


@dataclass(frozen=True)
class ModelEndpoint:
    """Upstream URL for a single model.

    Per-model credentials are deferred to Phase 3 (see plan). For v1
    the profile-level ``api_key_ref`` is the only credential and is
    used for every model in the profile.
    """

    url: str


@dataclass
class Subagent:
    """One entry in ``CLAUDE_CONFIG_DIR/agents/*.md``.

    Claude Code represents subagents as Markdown files with YAML
    frontmatter (fields ``name``, ``description``, ``tools``, ``model``)
    plus the file body as the system prompt. The dataclass here
    matches that schema 1:1 so :func:`write_profile_config` can render
    the file directly without further translation.

    ``tools`` and ``model`` are optional — Claude Code treats ``None``
    as "inherit defaults" — so we model them as nullable rather than
    empty-string.
    """

    name: str
    description: str
    system_prompt: str
    tools: list[str] | None = None
    model: str | None = None


@dataclass
class ClaudeProfile:
    """One Claude Code session profile.

    Attributes
    ----------
    name:
        Human-readable label shown in the menu dropdown.
    slug:
        Filesystem-safe identifier (derived from ``name`` via
        :func:`slugify`). Stable across renames so the on-disk
        ``CLAUDE_CONFIG_DIR`` directory isn't lost when the user
        edits the profile's display name.
    model_endpoints:
        Mapping ``model_name → upstream URL``. Drives both the proxy
        routing (``ProxyServerThread.model_endpoints``) and the
        ``availableModels`` list in ``settings.json``.
    default_model:
        ``"model"`` key in ``settings.json`` + ``ANTHROPIC_MODEL`` env
        var. Optional — leave ``None`` to use whatever Claude Code's
        own default is.
    api_key_ref:
        Reference into :class:`CredentialStore`. ``None`` means no
        credential is configured; the session then falls back to a
        labeled sentinel (``sk-proxy-injected``) so Claude Code
        doesn't refuse to start, and the proxy will reject requests
        until a real key is configured.
    env_vars:
        Per-session environment overrides layered on top of the
        base env by :func:`build_claude_session_env`.
    claude_settings:
        Free-form ``settings.json`` keys (permissions, hooks, etc.).
        Merged with the derived ``model`` / ``availableModels`` keys
        by :func:`write_profile_config` — derived keys win on
        collision so the profile UI can't accidentally clear the
        model allowlist.
    subagents:
        Each entry becomes one ``agents/<slug>.md`` file. Written on
        profile save (not per-session) so concurrent sessions of the
        same profile share the same agents.
    session_cwd:
        Optional default working directory for sessions of this
        profile. ``None`` means inherit the GUI process's cwd.
    """

    name: str
    slug: str
    model_endpoints: dict[str, str] = field(default_factory=dict)
    default_model: str | None = None
    api_key_ref: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    claude_settings: dict = field(default_factory=dict)
    subagents: list[Subagent] = field(default_factory=list)
    session_cwd: str | None = None