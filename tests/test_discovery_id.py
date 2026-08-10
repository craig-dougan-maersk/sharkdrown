"""Tests for the shared discovery-id helper.

The contract: ``/v1/models`` and ``settings.json.availableModels``
must use the SAME id format so Claude Code's picker filter applies
identically to both surfaces. The shared helper
``src/profiles/discovery_id`` is the single source of truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def test_picker_id_for_alias_claude_passthrough() -> None:
    """Claude aliases keep their bare name — the id already passes
    the picker filter (``claude`` / ``anthropic`` substring
    check) on its own."""
    from src.profiles.discovery_id import picker_id_for_alias

    # All of these should pass through verbatim.
    for alias in (
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "anthropic-claude",
        "claude",
        "Claude-Large",  # case-insensitive
        "ANTHROPIC-foo",
    ):
        assert picker_id_for_alias(alias) == alias, (
            f"{alias!r} should pass through verbatim (contains "
            f"claude/anthropic substring), got "
            f"{picker_id_for_alias(alias)!r}"
        )


def test_picker_id_for_alias_non_claude_gets_suffix() -> None:
    """Non-claude aliases get the synthetic ``-non-anthropic``
    suffix so their id passes Claude Code's picker filter."""
    from src.profiles.discovery_id import (
        SYNTHETIC_ALIAS_SUFFIX,
        picker_id_for_alias,
    )

    for alias in ("minimax", "mm", "freestuff", "team-pro", "sonnet"):
        expected = f"{alias}{SYNTHETIC_ALIAS_SUFFIX}"
        assert picker_id_for_alias(alias) == expected, (
            f"{alias!r} should get the synthetic suffix, got "
            f"{picker_id_for_alias(alias)!r}"
        )


def test_passes_claude_picker_filter_matches_claude_code_spec() -> None:
    """The filter predicate matches Claude Code's documented rule
    exactly: case-insensitive substring check for ``claude`` OR
    ``anthropic``."""
    from src.profiles.discovery_id import passes_claude_picker_filter

    # Passes (substring match).
    for name in ("claude", "claude-opus-4-8", "CLAUDE", "Claude-X",
                 "anthropic", "anthropic-claude", "ANTHROPIC-foo",
                 "Claude-Large"):
        assert passes_claude_picker_filter(name) is True, (
            f"{name!r} should pass the filter"
        )
    # Fails (no substring).
    for name in ("minimax", "mm", "freestuff", "opus", "sonnet",
                 "team-pro", "MiniMax-M3"):
        assert passes_claude_picker_filter(name) is False, (
            f"{name!r} should fail the filter"
        )


def test_settings_available_models_matches_proxy_v1_models_format() -> None:
    """The id format used by ``settings.json.availableModels`` must
    match what the proxy emits in ``/v1/models`` so Claude Code's
    picker filter applies identically to both surfaces.

    We construct a profile with a mix of claude, anthropic and
    non-claude aliases, run both the settings.json writer and the
    proxy's ``list_models`` handler, and assert the same id values
    appear in both surfaces."""
    import json
    import tempfile

    from ccproxy.app.api.proxy import list_models
    from ccproxy.app.config import (
        ModelEndpointsCfg,
        ProviderCfg,
        Settings,
    )
    from src.profiles import ClaudeProfile, ModelEndpoint, Provider
    from src.profiles.config_dir import write_profile_config

    profile = ClaudeProfile(
        name="Demo",
        slug="demo",
        providers={
            "default": Provider(
                name="default", url="https://api.example.com"
            ),
        },
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(
                provider_name="default",
                aliases=("opus",),  # non-claude → synthetic suffix
            ),
            "real-model": ModelEndpoint(
                provider_name="default",
                aliases=("minimax", "claude-mm"),  # mix of non-/claude
            ),
        },
    )

    # ---- 1. settings.json.availableModels ----
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        cfg = write_profile_config(profile, base_dir=Path(tmp))
        settings = json.loads(
            (cfg / "settings.json").read_text(encoding="utf-8")
        )
        settings_ids = set(settings["availableModels"])

    # ---- 2. /v1/models ids (after the proxy's augmentation, but the
    # augmentation happens in _proxy_lifecycle, not in list_models).
    # Here we manually replicate the augmentation so the handler sees
    # both the bare alias AND its synthetic sibling — same data the
    # handler sees in production.
    from src.ui.main_window._proxy_lifecycle import (
        augment_proxy_aliases_for_discovery,
    )

    proxy_aliases = {"opus": "claude-opus-4-8",
                     "minimax": "real-model",
                     "claude-mm": "real-model"}
    augment_proxy_aliases_for_discovery(proxy_aliases)

    proxy_cfg = Settings(
        model_endpoints=ModelEndpointsCfg(
            providers={"default": ProviderCfg(
                url="https://api.example.com",
                available_models=[],
            )},
            endpoints={
                "claude-opus-4-8": "default",
                "real-model": "default",
            },
            model_aliases=proxy_aliases,
        ),
    )

    class _State:
        debug_token = None

    class _App:
        state = _State()

    class _Req:
        query_params = {}
        app = _App()

    import asyncio

    body = asyncio.run(list_models(_Req(), settings=proxy_cfg)).body.decode()
    parsed = json.loads(body)
    proxy_ids = {entry["id"] for entry in parsed["data"]}

    # Settings.json should contain the BARE alias names; the picker
    # then shows them as ``/model minimax`` (the synthetic id is
    # only the routing target, the picker label is the bare alias
    # via ``display_name``).
    assert "opus-non-anthropic" in settings_ids, (
        f"non-claude alias should get synthetic suffix in "
        f"availableModels, got {settings_ids!r}"
    )
    assert "minimax-non-anthropic" in settings_ids
    assert "claude-mm" in settings_ids, (
        f"claude-substring alias should keep bare name in "
        f"availableModels, got {settings_ids!r}"
    )

    # /v1/models should advertise the SAME ids (so the picker
    # entries line up regardless of which surface Claude Code reads).
    assert "opus-non-anthropic" in proxy_ids, (
        f"/v1/models should advertise synthetic ids that match "
        f"availableModels, got proxy_ids={proxy_ids!r}, "
        f"settings_ids={settings_ids!r}"
    )
    assert "minimax-non-anthropic" in proxy_ids
    assert "claude-mm" in proxy_ids

    # Cross-check: every synthetic-id alias in settings.json must
    # ALSO appear in /v1/models (and vice versa) — otherwise the
    # picker would show the same entry twice or miss it entirely.
    settings_synthetic = {x for x in settings_ids if x.endswith("-non-anthropic")}
    proxy_synthetic = {x for x in proxy_ids if x.endswith("-non-anthropic")}
    assert settings_synthetic == proxy_synthetic, (
        f"synthetic-id sets must match between settings.json and "
        f"/v1/models; settings={settings_synthetic!r}, "
        f"proxy={proxy_synthetic!r}"
    )

    print("✓ settings.json.availableModels and /v1/models use the same id format")


def test_discovery_id_is_a_pure_function() -> None:
    """The helper is stateless — calling it twice with the same
    input gives the same output. This guards against accidental
    caching or memoisation creeping in."""
    from src.profiles.discovery_id import picker_id_for_alias

    for _ in range(3):
        assert picker_id_for_alias("minimax") == "minimax-non-anthropic"
        assert picker_id_for_alias("claude-opus") == "claude-opus"
    print("✓ picker_id_for_alias is deterministic")