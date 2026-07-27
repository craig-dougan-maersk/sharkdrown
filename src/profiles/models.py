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
class Provider:
    """A named upstream service (e.g. MiniMax, Anthropic, CoPilot).

    Providers are the stable identity for an upstream — endpoints
    (see :class:`ModelEndpoint`) reference a provider by ``name`` and
    resolve the URL *and* authentication through it. The provider
    also publishes the catalog of models it hosts; the Model
    Configuration tab constrains its Model dropdown to that catalog.

    Attributes
    ----------
    name:
        Stable, human-readable identifier (e.g. ``"Anthropic"``,
        ``"MiniMax"``). Used as the display label in the Model
        Configuration dropdown and as the key under which the
        provider is stored on the profile.
    url:
        Upstream URL the proxy routes requests to. Resolved at
        config-read time; the endpoint stores the *name*, not the
        URL, so a provider URL update propagates to every endpoint
        that names it.
    key_ref:
        Stable ref into the global :class:`CredentialStore`.
        ``None`` means no auth is attached to this provider — the
        Anthropic path forwards whatever ``x-api-key`` the client
        sent; the OpenAI path falls back to the proxy's configured
        ``OPENAI_API_KEY``. ``key_ref`` is the *stable*
        CredentialStore key (e.g. ``"default"``, ``"work"``), not a
        friendly display name, so renaming a key in the editor
        doesn't silently re-attach every endpoint that referenced
        the provider.
    available_models:
        Tuple of model names this provider hosts (e.g.
        ``("claude-opus-4-8", "claude-sonnet-4-6")``). The Model
        Configuration tab's per-row Model dropdown is populated
        from this list — the user can't route a model that isn't in
        some provider's catalog. Tuple (not list) because the
        dataclass is frozen; mutations replace the whole tuple.
    """

    name: str
    url: str
    key_ref: str | None = None
    available_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelEndpoint:
    """A model routed through a named :class:`Provider` + optional aliases.

    Authentication is *not* a per-endpoint concern — it's a property of
    the upstream identity, so the provider's ``key_ref`` authenticates
    every request that names it. The endpoint just names the
    provider and declares aliases.

    Attributes
    ----------
    provider_name:
        Stable name (key) of a :class:`Provider` on the profile. The
        endpoint's actual upstream URL *and* API key are resolved
        through the provider — editing the provider URL or key
        propagates to every endpoint that names it, no edits
        required here.
    aliases:
        Short, human-friendly names a client can use in place of the
        real model name. The proxy rewrites an incoming
        ``req.model = alias`` to the real model name before any
        routing or URL resolution runs, so clients can pass
        ``"opus"`` instead of ``"claude-opus-4-8"``. Empty tuple
        means "no aliases" — most endpoints.

        Aliases are *properties of the model they refer to* — moving
        an alias to a different real model is a one-line edit on this
        dataclass. Uniqueness across the profile is the editor's
        responsibility (``_on_accept`` refuses to save two real
        models that claim the same alias).
    """

    provider_name: str
    aliases: tuple[str, ...] = ()


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
    providers:
        Mapping ``provider_name → :class:`Provider` ``. Each
        provider carries its URL, an optional ``key_ref`` into the
        global :class:`CredentialStore`, and a catalog of
        ``available_models`` it hosts. Endpoints on this profile
        reference providers by ``name``; both the URL *and* the
        auth key resolve through this dict. The
        :class:`ProfileEditorDialog` "Providers" tab drives this
        field.
    model_endpoints:
        Mapping ``model_name → :class:`ModelEndpoint` ``. Each
        endpoint names a :class:`Provider` and declares aliases
        that the proxy rewrites to the real model name before
        routing. Authentication is *not* per-endpoint — it lives on
        the provider. Drives both the proxy routing
        (``ProxyServerThread.model_endpoints``) and the
        ``availableModels`` list in ``settings.json``.
    default_model:
        ``"model"`` key in ``settings.json`` + ``ANTHROPIC_MODEL`` env
        var. Optional — leave ``None`` to use whatever Claude Code's
        own default is.
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
    bind_address:
        Host the embedded proxy binds to. Defaults to ``127.0.0.1``
        (loopback only) so the desktop never exposes the proxy on the
        LAN by accident. ``"0.0.0.0"`` opts the proxy in to LAN
        exposure.
    port:
        TCP port the proxy binds to. ``0`` means "let the OS pick a
        free port" — the actual bound port is reported via
        ``ProxyServerThread.started_ok``.
    config_dir:
        Directory where the proxy's ``config.yaml`` and ``.env`` files
        live. Empty string means "auto-discover" (search data dir,
        then CWD, then the original source-proxy location).
    is_default:
        ``True`` for the always-present ``Default`` profile. The
        ``Default`` profile is non-deletable and acts as the template
        for new profiles. The flag is set on the seed / first save
        and preserved on rename; the ``Manage Profiles`` dialog uses
        it to disable the Delete button.

    Notes
    -----
    Credentials are no longer carried on the profile itself — they
    live in the global :class:`CredentialStore` and are referenced by
    ``ModelEndpoint.key_ref``. The previous ``api_key_ref`` field was
    dropped to support multiple keys per profile and per-endpoint
    credential selection.
    """

    name: str
    slug: str
    providers: dict[str, Provider] = field(default_factory=dict)
    model_endpoints: dict[str, ModelEndpoint] = field(default_factory=dict)
    default_model: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    claude_settings: dict = field(default_factory=dict)
    subagents: list[Subagent] = field(default_factory=list)
    session_cwd: str | None = None
    bind_address: str = "127.0.0.1"
    port: int = 3001
    config_dir: str = ""
    is_default: bool = False
