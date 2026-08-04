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
from typing import Mapping

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
        target (``/home/claude/.claude``). When ``False``, the
        host values pass through unchanged.
    """
    # ---- Docker rewrites -------------------------------------------------
    if container_aware:
        # Late import — ``docker_session`` transitively pulls in
        # PySide6, and env_builder is imported by modules that
        # need to work without a QApplication (e.g. the proxy).
        from src.ui.claude.docker_session import _IN_CONTAINER_CLAUDE_HOME

        proxy_base_url = _rewrite_proxy_for_container(proxy_base_url)
        config_dir = _IN_CONTAINER_CLAUDE_HOME

    env: dict[str, str] = os.environ.copy()
    """Return a fresh dict containing every env var the child needs.

    Parameters
    ----------
    profile
        The profile the session is locked to. Drives ``ANTHROPIC_MODEL``
        (from ``default_model``) and the per-profile ``env_vars``.
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
    """
    env: dict[str, str] = os.environ.copy()

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