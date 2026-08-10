"""Assemble the env dict for a Claude Code session.

Layered precedence (later wins on key collision):

1. ``os.environ.copy()`` — the GUI process's environment, including
   ``PATH``, ``HOME``, ``LANG``, etc. Built on this because
   :class:`src.ui.terminal.pty_backend.PtySession` calls
   ``os.execvpe`` with a *complete* replacement env.
2. **Infrastructure vars** (``ANTHROPIC_BASE_URL``,
   ``CLAUDE_CONFIG_DIR``, ``ANTHROPIC_API_KEY``, ``ANTHROPIC_MODEL``)
   computed from the profile + proxy URL. Always present.
3. ``profile.env_vars`` — user-configured additions.
4. ``session_env`` — caller-provided overrides (per-session env vars
   the user types at session-creation time, if we ever expose a UI
   for them; right now the parameter is unused but the precedence is
   already correct).

After layering, the infra vars are *re-applied* on top so a profile /
session that accidentally sets ``ANTHROPIC_BASE_URL`` doesn't break
proxy routing. A :class:`UserWarning` is emitted in that case — the
profile editor will refuse to save such a profile in a follow-up.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Mapping

from src.profiles import global_env
from src.profiles.credential_store import CredentialStore
from src.profiles.models import ClaudeProfile
from src.profiles.store import SENTINEL_API_KEY


# The set of env vars the proxy / Claude integration owns. If a profile
# or session env tries to set any of these we clobber back to the
# integration's value and warn — this is the safety net for the UI
# validation in the profile editor.
INFRA_ENV_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "TERM",
        "COLORTERM",
    }
)


def resolve_api_key(
    profile: ClaudeProfile,
    credential_store: CredentialStore,
) -> str | None:
    """Return the resolved API key for the profile's default model.

    The session launcher calls this once per session. It walks the
    profile's ``model_endpoints`` map, finds the entry for the
    profile's default model (read from ``claude_settings["model"]``,
    which is the on-disk key Claude Code reads), then resolves the
    *provider's* ``key_ref`` to a plaintext value via
    :class:`CredentialStore`.

    ``claude_settings["model"]`` may hold either the canonical
    upstream name (``"MiniMax-M3"``) **or** a user-friendly alias
    (``"minimax"``). The profile editor saves whichever the user
    typed in the Default Model combo; both should resolve to the
    same credential. We try the canonical lookup first, then walk
    the endpoints' ``aliases`` for a match — the same pattern the
    proxy uses to rewrite inbound model names.

    Authentication is now a property of the provider rather than
    the endpoint (post-refactor) — so two models on the same
    provider share the same key, and the resolver traverses the
    ``endpoint → provider → key_ref`` chain.

    Returns ``None`` if the profile has no default model, the
    model isn't in ``model_endpoints``, the provider can't be
    looked up, the provider has no ``key_ref``, or the ref doesn't
    resolve to a stored value. In all of these cases
    :func:`build_claude_session_env` substitutes the labeled
    sentinel so Claude Code still launches.

    Parameters
    ----------
    profile
        The profile the session is locked to.
    credential_store
        Where named keys live. May return ``None`` if the named key
        has been deleted externally; this function surfaces that
        cleanly rather than raising.
    """
    settings = getattr(profile, "claude_settings", {}) or {}
    default_model = (
        settings.get("model") if isinstance(settings, dict) else None
    )
    if not default_model:
        return None
    endpoint = profile.model_endpoints.get(default_model)
    # Fallback: ``claude_settings["model"]`` may be an alias
    # (``"minimax"``) rather than the canonical upstream name
    # (``"MiniMax-M3"``). Walk the endpoints' aliases for a match.
    # The proxy's ``_apply_alias_rewrite`` does the same thing on
    # inbound requests; we mirror it here so the credential
    # resolver and the request handler agree on which provider an
    # alias refers to.
    if endpoint is None:
        for model_name, ep in profile.model_endpoints.items():
            if ep is None or isinstance(ep, str):
                continue
            aliases = getattr(ep, "aliases", ()) or ()
            if default_model in aliases:
                endpoint = ep
                break
    if endpoint is None:
        return None
    provider = profile.providers.get(endpoint.provider_name)
    if provider is None or not provider.key_ref:
        return None
    return credential_store.get(provider.key_ref)


def build_claude_session_env(
    *,
    profile: ClaudeProfile,
    proxy_base_url: str,
    config_dir: str,
    api_key: str | None,
    session_env: Mapping[str, str] | None = None,
    container_aware: bool = False,
    data_dir: Path | None = None,
) -> dict[str, str]:
    """Return a fresh dict containing every env var the child needs.

    Parameters
    ----------
    profile
        The profile the session is locked to. Drives ``ANTHROPIC_MODEL``
        (from ``claude_settings["model"]``) and the per-profile
        ``env_vars``.
    proxy_base_url
        Where Claude Code should point — typically
        ``http://127.0.0.1:<port>``. The caller resolves this; the
        env builder doesn't talk to the proxy directly.
    config_dir
        Absolute path to the per-profile ``CLAUDE_CONFIG_DIR``
        (the return value of ``write_profile_config``).
    api_key
        Resolved API key for the profile (``CredentialStore.get(ref)``).
        ``None`` → uses the labeled sentinel ``sk-proxy-injected`` so
        Claude Code doesn't refuse to start; the proxy will reject
        requests until a real key is configured.
    session_env
        Optional per-session overrides layered above ``profile.env_vars``.
    container_aware
        When ``True`` (Docker-mode sessions), rewrites
        ``ANTHROPIC_BASE_URL`` so the host portion points at
        ``host.docker.internal`` (the Docker-for-Mac/Win hostname
        that resolves to the host machine), and rewrites
        ``CLAUDE_CONFIG_DIR`` to the in-container bind-mount
        target (``/home/claude/.claude``). The bind-mount
        sources the whole profile dir so ``<host>/<slug>/.claude/``
        appears at ``/home/claude/.claude/`` inside the
        container — where Claude Code reads its config. When
        ``False``, the host values pass through unchanged.
    data_dir
        Optional path to the app data directory (``<data_dir>`` from
        ``proxy.paths.data_dir()``). When provided, the global env
        file (``<data_dir>/defaults.env``) is loaded and layered
        above ``session_env`` but below the safety-net re-apply of
        integration-owned infra vars. When ``None`` (the default for
        legacy callers that don't pass it), the global layer is
        skipped — existing tests and call sites that don't yet
        thread ``data_dir`` are unaffected. New callers (the
        desktop app) should always pass ``data_dir`` so the
        shipped defaults (``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY``
        and ``CLAUDE_CODE_NO_FLICKER``) reach the session.
    """
    # ---- Docker rewrites -------------------------------------------------
    if container_aware:
        # Late import — ``docker_session`` transitively pulls in
        # PySide6, and env_builder is imported by modules that
        # need to work without a QApplication (e.g. the proxy).
        from src.ui.claude.docker_session import (
            _IN_CONTAINER_CLAUDE_CONFIG_DIR,
        )

        proxy_base_url = _rewrite_proxy_for_container(proxy_base_url)
        config_dir = _IN_CONTAINER_CLAUDE_CONFIG_DIR

    env: dict[str, str] = os.environ.copy()

    # ---- strip inherited infra vars --------------------------------------
    # The GUI process starts from ``os.environ`` (PATH, HOME, LANG,
    # etc.) but for *infrastructure* vars (those the proxy / Claude
    # integration owns) the integration's own write below is the
    # only source of truth. Without this strip, a value the user's
    # interactive shell happens to have — e.g.
    # ``ANTHROPIC_MODEL=MiniMax-M3`` set by ``direnv`` or a
    # previous shell experiment — silently leaks into every
    # Claude Code session. Setting "Default model: none" in the
    # profile editor wouldn't matter: the inherited value still
    # arrives in the child, indistinguishable from one the
    # profile explicitly chose. Strip the entire ``INFRA_ENV_VARS``
    # set here; the safety-net re-apply below then writes the
    # integration's values (or omits ``ANTHROPIC_MODEL`` when the
    # profile has no default model).
    #
    # We don't touch non-infra vars (``PATH`` etc.) — those come
    # from the GUI process by design and the shell's value is
    # what the user wants.
    for k in INFRA_ENV_VARS:
        env.pop(k, None)

    # ---- infra vars -------------------------------------------------------
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["ANTHROPIC_BASE_URL"] = proxy_base_url
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["ANTHROPIC_API_KEY"] = api_key if api_key else SENTINEL_API_KEY
    # Claude Code prefers ``ANTHROPIC_AUTH_TOKEN`` over ``ANTHROPIC_API_KEY``
    # when both are present — the auth token sets ``Authorization: Bearer``
    # rather than ``x-api-key``, which proxy gateways (e.g. MiniMax) expect.
    # Without this, container-launched sessions authenticate as
    # ``x-api-key`` only and MiniMax-style gateways reject the request.
    # Same value as the API key: the resolved credential serves both roles.
    env["ANTHROPIC_AUTH_TOKEN"] = api_key if api_key else SENTINEL_API_KEY
    settings = getattr(profile, "claude_settings", {}) or {}
    default_model = (
        settings.get("model") if isinstance(settings, dict) else None
    )
    if default_model:
        env["ANTHROPIC_MODEL"] = default_model

    # ---- profile / session overrides --------------------------------------
    clobbers: list[str] = []
    for k, v in profile.env_vars.items():
        if k in INFRA_ENV_VARS and env.get(k) != v:
            clobbers.append(k)
        env[str(k)] = str(v)
    if session_env:
        for k, v in session_env.items():
            if k in INFRA_ENV_VARS and env.get(k) != v:
                if k not in clobbers:
                    clobbers.append(k)
            env[str(k)] = str(v)

    # ---- global env: load ``<data_dir>/defaults.env`` ----------------------
    # The shipped global env (e.g. ``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY``
    # and ``CLAUDE_CODE_NO_FLICKER``) sits above ``session_env`` so a
    # user-edited ``defaults.env`` wins over per-session overrides, but
    # BELOW the safety-net so an ``defaults.env`` that tries to
    # redirect ``ANTHROPIC_BASE_URL`` (or any other infra var) is a
    # no-op — the safety-net re-apply below restores the
    # integration's value.
    #
    # ``data_dir`` is optional so legacy callers (and the existing
    # test suite that doesn't thread a data dir) keep working
    # unchanged; the desktop app always passes it. We swallow any
    # I/O error here so a corrupt or missing ``defaults.env`` cannot
    # prevent Claude Code from launching — better to ship without
    # the two new vars than to fail the whole session.
    if data_dir is not None:
        try:
            global_defaults = global_env.load_global_env(Path(data_dir))
        except OSError as exc:
            warnings.warn(
                f"Could not load global env from {data_dir}: {exc}; "
                "skipping the global env layer.",
                UserWarning,
                stacklevel=2,
            )
            global_defaults = {}
        for k, v in global_defaults.items():
            # Track (but don't act on) clobbers here — the
            # safety-net below fixes them if the global env tried
            # to override an infra var.
            if k in INFRA_ENV_VARS and env.get(k) != v:
                if k not in clobbers:
                    clobbers.append(k)
            env[str(k)] = str(v)

    # ---- TUI mode override (Bug A) ---------------------------------------
    # ``defaults.env`` ships ``CLAUDE_CODE_NO_FLICKER=1``, which
    # forces Claude Code (>= 2.1.110) into its fullscreen / alt-
    # screen flicker-free rendering. The user reported that "the
    # /tui setting for Claude code sessions seems to be fullscreen
    # instead of default" — they didn't realise the env var was
    # doing it.
    #
    # The ``tui_mode`` field on the global settings store
    # (``claude_global.json``) is the user-facing escape hatch.
    # We honour it HERE, after the defaults.env layer, so users
    # can opt out of fullscreen mode without hand-editing
    # ``defaults.env``. Default value (``"fullscreen"``) preserves
    # the historic behaviour; users who pick ``"default"`` or
    # ``"off"`` get Claude Code's standard non-fullscreen mode.
    #
    # The override is read from the global settings store (not
    # threaded as a parameter) to keep the call sites stable;
    # ``claude_global.json`` is the single source of truth.
    if data_dir is not None:
        try:
            from src.profiles.global_settings import (
                GLOBAL_SETTINGS_FILENAME,
                GlobalSettingsStore,
            )

            global_settings_path = Path(data_dir) / GLOBAL_SETTINGS_FILENAME
            if global_settings_path.is_file():
                tui_store = GlobalSettingsStore(global_settings_path)
                tui_mode = tui_store.load().tui_mode
            else:
                # No ``claude_global.json`` yet — fall through to the
                # defaults.env behaviour (fullscreen). The store will
                # be created on first save.
                tui_mode = "fullscreen"
        except (OSError, ValueError) as exc:
            warnings.warn(
                f"Could not read tui_mode from global settings: {exc}; "
                "using defaults.env behaviour.",
                UserWarning,
                stacklevel=2,
            )
            tui_mode = "fullscreen"
        if tui_mode in ("default", "off"):
            # Drop the flicker-free flag regardless of what
            # ``defaults.env`` set. ``None`` is meaningful — it
            # tells Claude Code to use its built-in default
            # rendering path (non-fullscreen).
            env.pop("CLAUDE_CODE_NO_FLICKER", None)
        # ``"fullscreen"`` leaves the defaults.env flag in place.

    # ---- safety net: re-apply infra vars on top ---------------------------
    if clobbers:
        warnings.warn(
            f"Profile or session env clobbered infra vars {clobbers!r}; "
            "the integration's values have been re-applied.",
            UserWarning,
            stacklevel=2,
        )
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["ANTHROPIC_BASE_URL"] = proxy_base_url
        env["CLAUDE_CONFIG_DIR"] = config_dir
        env["ANTHROPIC_API_KEY"] = api_key if api_key else SENTINEL_API_KEY
        env["ANTHROPIC_AUTH_TOKEN"] = api_key if api_key else SENTINEL_API_KEY
        settings = getattr(profile, "claude_settings", {}) or {}
        default_model = (
            settings.get("model") if isinstance(settings, dict) else None
        )
        if default_model:
            env["ANTHROPIC_MODEL"] = default_model

    return env


def _rewrite_proxy_for_container(url: str) -> str:
    """Rewrite ``http://127.0.0.1:PORT`` to use the docker host alias.

    Docker on macOS / Windows maps ``host.docker.internal`` to the
    host's loopback. On Linux the same alias works once
    ``--add-host host.docker.internal:host-gateway`` is passed to
    ``docker create``; :class:`DockerSession` does that
    implicitly.

    Parses with :mod:`urllib.parse` so the port and path are
    preserved verbatim — only the hostname is replaced. URLs that
    don't parse cleanly (no scheme, no host) pass through
    unchanged; the container will fail to reach the proxy and the
    session will surface the error.
    """
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.netloc:
        return url
    userinfo = ""
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.split("@", 1)[0] + "@"
    port = parsed.port
    new_netloc = f"{userinfo}host.docker.internal"
    if port is not None:
        new_netloc = f"{new_netloc}:{port}"
    return urlunparse(parsed._replace(netloc=new_netloc))