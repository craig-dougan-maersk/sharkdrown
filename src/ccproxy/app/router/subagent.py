"""Subagent routing.

Claude Code lets users define specialist subagents via ``.claude/agents/<name>.md``
files. When the assistant spawns a subagent, the proxy detects it (by hashing
the agent's static prompt — the part before any ``Notes:`` line) and routes
the request to a configured model/provider.

When ``subagents.enable`` is ``False`` the router falls back to plain
provider-pattern routing (look at the requested model name and pick the
matching provider).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ccproxy.app.config import Settings
from ccproxy.app.models.anthropic import AnthropicRequest, AnthropicSystemMessage
from ccproxy.app.providers.base import Provider

logger = logging.getLogger("claude_code_proxy.router.subagent")


# ---- Provider pattern table -------------------------------------------------

# First match wins. Order matters: longer prefixes before shorter ones.
# Entries ending with "-" are pure prefixes; entries without "-" require
# an exact match OR that the model continues with "-" (e.g., "o1-mini"
# matches but "oracle-3b" does not).
ProviderPattern = tuple[str, str]  # (pattern, provider_name)
_PROVIDER_PATTERNS: tuple[ProviderPattern, ...] = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o1", "openai"),
    ("o3-", "openai"),
    ("o3", "openai"),
    ("o4-", "openai"),
    ("o4", "openai"),
    ("claude-", "anthropic"),
)


def provider_for_model(model: str) -> str:
    """Return the provider name for a given model identifier.

    Falls back to ``"anthropic"`` for unknown models (matches the upstream
    Go router's behaviour).
    """
    for pattern, name in _PROVIDER_PATTERNS:
        if pattern.endswith("-"):
            if model.startswith(pattern):
                return name
        else:
            if model == pattern or model.startswith(pattern + "-"):
                return name
    return "anthropic"


# ---- Hashing helpers --------------------------------------------------------


def hash_static_prompt(prompt: str) -> str:
    """SHA256[:16] of the static portion of a prompt.

    The first 16 hex chars (8 bytes) provide 2^64 collision space — enough
    to differentiate hundreds of distinct agents with negligible collision
    probability.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


# ---- Static-prompt extraction ----------------------------------------------


_NOTES_SINGLE = "\nNotes:"


def extract_static_prompt(system_prompt: str) -> str:
    """Return the static portion of an agent's system prompt.

    Strips everything from the first ``\\nNotes:`` line onwards, since the
    ``Notes:`` section is dynamic content Claude Code injects at runtime and
    would invalidate the hash. ``\\n\\nNotes:`` always contains ``\\nNotes:``
    as a substring, so a single find() covers both separators.
    """
    idx = system_prompt.find(_NOTES_SINGLE)
    if idx != -1:
        return system_prompt[:idx].strip()
    return system_prompt.strip()


# ---- Agent file loading ----------------------------------------------------

# Format: ``<metadata>\n---\n<system prompt>\n`` (the front-matter delimiter
# is ``\n---\n``).
_AGENT_DELIM = "\n---\n"


def _read_agent_file(agent_name: str, search_dirs: list[Path]) -> str | None:
    """Read a single agent's static prompt from the first matching path.

    Tries each ``search_dir`` in order. Returns ``None`` if no file is
    found; the caller logs a warning and continues.
    """
    for d in search_dirs:
        path = d / f"{agent_name}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def _search_dirs(cwd: Path | None) -> list[Path]:
    """Build the ordered list of agent-file search directories."""
    import os

    home = Path(os.path.expanduser("~"))
    dirs: list[Path] = [home / ".claude" / "agents"]
    if cwd is not None:
        dirs.insert(0, cwd / ".claude" / "agents")
    return dirs


def _search_dirs_for_tests(cwd: Path | None, *, home: Path) -> list[Path]:
    """Test-friendly version that takes the home dir explicitly."""
    dirs: list[Path] = [home / ".claude" / "agents"]
    if cwd is not None:
        dirs.insert(0, cwd / ".claude" / "agents")
    return dirs


def load_custom_agents(
    settings: Settings,
    providers: dict[str, Provider],
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> dict[str, tuple[str, str, Provider]]:
    """Precompute ``{prompt_hash: (target_model, target_provider, Provider)}``.

    Returns a mapping keyed by the truncated SHA256 of each agent's static
    prompt. Missing agent files produce a warning log line and are skipped
    (the agent mapping is treated as a no-op rather than a fatal error).
    """
    import os

    resolved_home = home or Path(os.path.expanduser("~"))
    search_dirs: list[Path] = [resolved_home / ".claude" / "agents"]
    if cwd is not None:
        search_dirs.insert(0, cwd / ".claude" / "agents")

    out: dict[str, tuple[str, str, Provider]] = {}
    mappings = settings.subagents.mappings or {}
    if not settings.subagents.enable:
        return out

    for agent_name, target_model in mappings.items():
        raw = _read_agent_file(agent_name, search_dirs)
        if raw is None:
            logger.warning(
                "subagent mapping %r -> %r: no .md found in any of %s",
                agent_name,
                target_model,
                search_dirs,
            )
            continue
        # Split on the front-matter delimiter; take the body.
        idx = raw.find(_AGENT_DELIM)
        if idx == -1:
            logger.warning(
                "subagent %r: missing '---' delimiter in %s; using whole file",
                agent_name,
                search_dirs,
            )
            body = raw
        else:
            body = raw[idx + len(_AGENT_DELIM) :]
        static = extract_static_prompt(body)
        h = hash_static_prompt(static)
        target_provider = provider_for_model(target_model)
        provider = providers.get(target_provider)
        if provider is None:
            logger.warning(
                "subagent %r -> %r: no provider registered for %s; skipping",
                agent_name,
                target_model,
                target_provider,
            )
            continue
        out[h] = (target_model, target_provider, provider)
        logger.info(
            "subagent %r registered: hash=%s -> %s on %s",
            agent_name,
            h,
            target_model,
            target_provider,
        )
    return out


# ---- Routing decision ------------------------------------------------------

# The literal strings used to tag each decision in storage.
RoutePath = Literal["passthrough", "converted", "subagent"]


class RouteDecision(BaseModel):
    """The router's output for an inbound request."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: Provider
    original_model: str
    target_model: str
    path: RoutePath
    target_provider_name: str


class SubagentRouter:
    """Inspect each Anthropic request and choose a provider + target model."""

    _REFRESH_INTERVAL_S = 30.0

    def __init__(
        self,
        settings: Settings,
        providers: dict[str, Provider],
        *,
        cwd: Path | None = None,
        home: Path | None = None,
    ) -> None:
        import time

        self._settings = settings
        self._providers = providers
        self._cwd = cwd
        self._home = home
        # Precomputed {prompt_hash: (target_model, target_provider, provider)}.
        self._custom_agents: dict[str, tuple[str, str, Provider]] = load_custom_agents(
            settings, providers, cwd=cwd, home=home
        )
        self._last_refresh_time = time.monotonic()
        self._agents_sig = self._agents_signature()

    def _agents_search_dirs(self) -> list[Path]:
        import os
        resolved_home = self._home or Path(os.path.expanduser("~"))
        dirs: list[Path] = [resolved_home / ".claude" / "agents"]
        if self._cwd is not None:
            dirs.insert(0, self._cwd / ".claude" / "agents")
        return dirs

    def _agents_signature(self) -> frozenset[tuple[Path, float]]:
        """Return a frozenset of (path, mtime) for all agent .md files.

        Any addition, deletion, or modification produces a different frozenset,
        so equality comparison detects all three cases — unlike a bare max-mtime
        check which misses deletions (remaining files have a lower or equal max).
        """
        result: set[tuple[Path, float]] = set()
        for d in self._agents_search_dirs():
            if not d.is_dir():
                continue
            for f in d.glob("*.md"):
                try:
                    result.add((f, f.stat().st_mtime))
                except OSError:
                    pass
        return frozenset(result)

    def _maybe_refresh_agents(self) -> None:
        """Refresh the agent hash cache if the set of agent files has changed."""
        import time

        now = time.monotonic()
        if now - self._last_refresh_time < self._REFRESH_INTERVAL_S:
            return
        self._last_refresh_time = now

        current_sig = self._agents_signature()
        if current_sig != self._agents_sig:
            logger.info("agent files changed; refreshing hashes")
            self._custom_agents = load_custom_agents(
                self._settings, self._providers, cwd=self._cwd, home=self._home
            )
            self._agents_sig = current_sig

    @property
    def custom_agent_count(self) -> int:
        return len(self._custom_agents)

    def determine_route(self, req: AnthropicRequest) -> RouteDecision:
        """Pick the provider and target model for one inbound request."""
        model = req.model

        # Periodically check whether agent files have been edited.
        self._maybe_refresh_agents()

        # Subagent detection — only when enabled.
        if self._settings.subagents.enable:
            decision = self._maybe_route_subagent(req)
            if decision is not None:
                return decision

        # User-defined endpoint routing (highest priority). The
        # desktop app populates ``model_endpoints.endpoints`` from
        # the active profile's ``model_endpoints`` map, so a
        # ``MiniMax-M3 → MiniMax`` entry there routes ``MiniMax-M3``
        # to the user's ``MiniMax`` provider — *not* to the
        # hardcoded default ``anthropic`` provider at api.anthropic.com.
        # Without this check, every unknown-model request would
        # silently land on api.anthropic.com and Anthropic would
        # reject it as "model not found".
        endpoints_cfg = self._settings.model_endpoints
        provider_name = (
            (endpoints_cfg.endpoints.get(model) or "")
            if endpoints_cfg
            else ""
        )
        if not provider_name and endpoints_cfg:
            provider_name = endpoints_cfg.default_provider or ""

        # Fall back to prefix-based routing for models the user
        # hasn't explicitly mapped (e.g. ``claude-opus-4-8`` →
        # anthropic, ``gpt-5`` → openai). ``provider_for_model``
        # returns ``"anthropic"`` as the catch-all for unknown
        # prefixes.
        if not provider_name:
            provider_name = provider_for_model(model)
        provider = self._providers.get(provider_name)
        if provider is None:
            # Last-ditch fallback: anthropic.
            provider = self._providers.get("anthropic")
            provider_name = "anthropic"

        return RouteDecision(
            provider=provider,
            original_model=model,
            target_model=model,
            path="passthrough" if provider_name == "anthropic" else "converted",
            target_provider_name=provider_name,
        )

    def _maybe_route_subagent(self, req: AnthropicRequest) -> RouteDecision | None:
        """Return a subagent override decision if the request matches a configured agent."""
        system = req.system
        if system is None:
            return None
        # Hard requirement: Claude Code currently sends exactly two system messages.
        if not isinstance(system, list) or len(system) != 2:
            return None
        first, second = system[0], system[1]
        if not (
            isinstance(first, AnthropicSystemMessage) and isinstance(second, AnthropicSystemMessage)
        ):
            return None
        if "You are Claude Code" not in first.text:
            return None
        static = extract_static_prompt(second.text)
        h = hash_static_prompt(static)
        match = self._custom_agents.get(h)
        if match is None:
            return None
        target_model, target_provider_name, provider = match
        return RouteDecision(
            provider=provider,
            original_model=req.model,
            target_model=target_model,
            path="subagent",
            target_provider_name=target_provider_name,
        )
