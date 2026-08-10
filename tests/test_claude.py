"""Tests for the Claude Code integration in sharkdrownv3.

Headless tests cover:
  - ``src.profiles`` — slug, credential store, profile store,
    config-dir writer, env builder
  - ``src.ui.terminal.terminal_session_view`` — env/cwd forwarding
  - ``src.ui.claude.claude_container_view`` — proxy deferral,
    claude-binary guard, session launching

Each test prints a checkmark on success and a traceback on failure.
The runner at the bottom iterates a fixed list of tests — same style
as ``test_terminal.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import warnings
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


# ---- helpers --------------------------------------------------------------


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _spin(ms: int = 200):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    qt = QTimer()
    qt.setSingleShot(True)
    qt.setInterval(ms)
    qt.timeout.connect(loop.quit)
    qt.start()
    loop.exec()


def _tmp_dir() -> Path:
    """Fresh tmpdir that gets cleaned up on process exit."""
    return Path(tempfile.mkdtemp(prefix="sharkdrown-claude-test-"))


# ---- slug ------------------------------------------------------------------


def test_slugify_basic():
    from src.profiles import slugify

    assert slugify("Default") == "default"
    assert slugify("My Profile") == "my-profile"
    assert slugify("Work-Server") == "work-server"
    assert slugify("work_server") == "work-server"
    print("✓ slugify handles typical names")


def test_slugify_collapses_punctuation():
    from src.profiles import slugify

    assert slugify("Foo!! @@@ Bar") == "foo-bar"
    assert slugify("__init__") == "init"
    print("✓ slugify collapses punctuation")


def test_slugify_empty_falls_back():
    from src.profiles import slugify

    assert slugify("") == "profile"
    assert slugify("   ") == "profile"
    assert slugify("---") == "profile"
    assert slugify("!@#$") == "profile"
    print("✓ slugify falls back for empty / punctuation-only input")


# ---- credential store -----------------------------------------------------


def test_credential_store_roundtrip():
    cs_path = _tmp_dir() / "credentials.json"
    from src.profiles import CredentialStore

    cs = CredentialStore(cs_path)
    assert cs.get("default-key") is None
    cs.set("default-key", "sk-test-1")
    assert cs.get("default-key") == "sk-test-1"
    cs.set("alt", "sk-test-2")
    assert cs.all_refs() == {"default-key": "sk-test-1", "alt": "sk-test-2"}
    cs.delete("default-key")
    assert cs.get("default-key") is None
    assert cs.get("alt") == "sk-test-2"
    print("✓ CredentialStore set/get/delete roundtrip")


def test_credential_store_chmod_600():
    cs_path = _tmp_dir() / "credentials.json"
    from src.profiles import CredentialStore

    cs = CredentialStore(cs_path)
    cs.set("k", "v")
    mode = stat.S_IMODE(cs_path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    print("✓ CredentialStore enforces chmod 600 on save")


def test_credential_store_corrupt_file_treated_as_empty():
    cs_path = _tmp_dir() / "credentials.json"
    cs_path.write_text("not json", encoding="utf-8")
    from src.profiles import CredentialStore

    cs = CredentialStore(cs_path)
    assert cs.get("any") is None
    # Writing on top repairs it.
    cs.set("k", "v")
    assert cs.get("k") == "v"
    print("✓ CredentialStore recovers from corrupt file")


# ---- profile store ---------------------------------------------------------


def _make_profile(name: str = "Test") -> "ClaudeProfile":
    from src.profiles import ClaudeProfile, ModelEndpoint, Provider

    return ClaudeProfile(
        name=name,
        slug=name.lower(),
        providers={
            "default": Provider(name="default", url="https://api.example.com"),
        },
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(provider_name="default"),
        },
        default_model="claude-opus-4-8",
        env_vars={"FOO": "bar"},
    )


def test_profile_store_roundtrip():
    from src.profiles import ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    p = _make_profile("Alice")
    store.upsert(p)
    loaded = store.load()
    # ``load()`` always materialises a Default profile (the user
    # contract: there's always a default at the top of the list),
    # so the persisted list now contains both the Default and Alice.
    assert len(loaded) == 2
    assert loaded[0].name == "Default", (
        f"Default profile should be at index 0, got {loaded[0].name!r}"
    )
    by_name = {p.name: p for p in loaded}
    assert by_name["Alice"].model_endpoints == p.model_endpoints
    assert by_name["Alice"].env_vars == p.env_vars
    # The Default profile stays active when a new profile is added —
    # the user opts in by switching via the menu. This matches the
    # "seeded from default" semantics: a new profile is just a
    # starting point, not an auto-activation.
    assert store.get_active_name() == "Default"
    print("✓ ProfileStore save/load roundtrip preserves fields")


def test_profile_store_migration_seeds_default_from_legacy_config():
    from src.profiles import ModelEndpoint, ProfileStore

    base = _tmp_dir()

    # Legacy config provider — pretend QSettings has these URLs.
    class _FakeCfg:
        model_endpoints = {
            "claude-opus-4-8": "https://api.anthropic.com",
            "claude-sonnet-4-6": "https://api.example.com",
        }

    store = ProfileStore(
        path=base / "profiles.json",
        legacy_proxy_config_provider=lambda: _FakeCfg(),
    )
    profiles = store.load()
    assert len(profiles) == 1
    assert profiles[0].name == "Default"
    # Legacy model_endpoints become providers (one per unique URL)
    # plus endpoints referencing them by name. The keys survive
    # verbatim and route through the new ``provider_name`` indirection.
    assert profiles[0].model_endpoints == {
        "claude-opus-4-8": ModelEndpoint(provider_name="api-anthropic-com"),
        "claude-sonnet-4-6": ModelEndpoint(provider_name="api-example-com"),
    }
    # Active profile should be Default after migration.
    assert store.get_active_name() == "Default"
    print("✓ ProfileStore seeds Default from legacy ProxyConfig on first launch")


def test_profile_store_migration_idempotent():
    from src.profiles import ProfileStore

    base = _tmp_dir()

    class _FakeCfg:
        model_endpoints = {"claude-opus-4-8": "https://api.anthropic.com"}

    store = ProfileStore(
        path=base / "profiles.json",
        legacy_proxy_config_provider=lambda: _FakeCfg(),
    )
    first = store.load()
    second = store.load()
    # Same number, same active name — migration didn't double-seed.
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].name == second[0].name == "Default"
    print("✓ ProfileStore migration is idempotent (no double-seed)")


def test_profile_store_upsert_replaces_existing():
    from src.profiles import ClaudeProfile, ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.upsert(_make_profile("Alpha"))
    # Edit Alpha — same name, different env_vars.
    edited = _make_profile("Alpha")
    edited.env_vars = {"FOO": "updated"}
    store.upsert(edited)
    profiles = store.load()
    # Default is auto-prepended; Alpha is the only user profile.
    by_name = {p.name: p for p in profiles}
    assert by_name["Alpha"].env_vars == {"FOO": "updated"}
    print("✓ ProfileStore.upsert replaces by name")


def test_profile_store_delete_promotes_remaining_to_active():
    from src.profiles import ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.upsert(_make_profile("First"))
    store.upsert(_make_profile("Second"))
    store.set_active("First")
    assert store.get_active_name() == "First"
    store.delete("First")
    # With Default always present, the new active is the first
    # remaining profile — which is "Default" (the always-on template).
    assert store.get_active_name() == "Default"
    print("✓ ProfileStore.delete falls back to Default as active")


def test_profile_store_dedups_duplicate_slugs():
    from src.profiles import ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.upsert(_make_profile("My Profile"))
    store.upsert(_make_profile("My-Profile"))  # same slug after slugify
    store.upsert(_make_profile("my profile"))  # same slug again
    profiles = store.load()
    # Default is auto-prepended in addition to the three user profiles.
    slugs = [p.slug for p in profiles if p.name != "Default"]
    assert len(slugs) == 3
    assert len(set(slugs)) == 3, f"slugs collided: {slugs}"
    print("✓ ProfileStore dedups duplicate slugs (preserves name, suffixes slug)")


def test_profile_store_chmod_600():
    from src.profiles import ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.upsert(_make_profile())
    mode = stat.S_IMODE((base / "profiles.json").stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    print("✓ ProfileStore enforces chmod 600 on profiles.json")


# ---- config-dir writer ----------------------------------------------------


def test_write_profile_config_writes_settings_json():
    """The ``model`` and free-form ``claude_settings`` keys round-trip;
    ``availableModels`` is omitted when no endpoint has aliases.
    Real upstream IDs (``claude-opus-4-8``) are deliberately *not*
    surfaced — only human-friendly aliases."""
    from src.profiles import ClaudeProfile, write_profile_config

    base = _tmp_dir()
    p = ClaudeProfile(
        name="Demo",
        slug="demo",
        model_endpoints={
            "claude-opus-4-8": "https://api.example.com",
            "claude-sonnet-4-6": "https://api.example.com",
        },
        default_model="claude-opus-4-8",
        claude_settings={"permissions": {"allow": ["Edit"]}},
    )
    config_dir = write_profile_config(p, base_dir=base)
    # The config dir is rooted at ``<slug>/.claude/`` so the bind-mount
    # source ``<slug>/`` lands it at ``/home/claude/.claude/`` inside
    # the container. See ``test_ensure_container_binds_profile_dir_to_home``.
    assert config_dir == base / "demo" / ".claude", (
        f"write_profile_config must return <slug>/.claude for "
        f"container bind-mount; got {config_dir!r}"
    )
    settings_path = config_dir / "settings.json"
    assert settings_path.is_file()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "claude-opus-4-8"
    # No aliases declared → ``availableModels`` key is omitted from
    # the file (vs. emitted as an empty list). The real names
    # ``claude-opus-4-8`` / ``claude-sonnet-4-6`` are not exposed.
    assert "availableModels" not in settings
    assert settings["permissions"] == {"allow": ["Edit"]}
    print("✓ write_profile_config emits settings.json with model + free-form keys")


def test_write_profile_config_exposes_aliases_in_available_models():
    """Per-endpoint aliases land in ``settings.json.availableModels`` so
    Claude Code's ``/model`` picker offers them. Real upstream IDs
    (``claude-opus-4-8``) stay hidden — only human-friendly aliases
    are surfaced. The proxy's alias-rewrite layer handles substitution
    on the wire.

    Multi-bug-fix-milestone contract: the id format MUST match
    what the proxy advertises in ``/v1/models``. Claude Code's
    picker applies the same ``id contains "claude" or "anthropic"``
    substring filter to both surfaces, so the format has to be
    identical or the picker shows inconsistent entries. The
    transformation lives in
    ``src/profiles/discovery_id.picker_id_for_alias`` and is
    applied uniformly to every alias before it's written.

    For aliases that already pass the filter (``claude-*`` or
    ``anthropic-*``) the bare name is written verbatim. For aliases
    that don't, a synthetic ``<alias>-non-anthropic`` suffix is
    appended so the picker keeps them.
    """
    from src.profiles import ClaudeProfile, ModelEndpoint, Provider, write_profile_config

    base = _tmp_dir()
    p = ClaudeProfile(
        name="Demo",
        slug="demo",
        providers={
            "default": Provider(
                name="default", url="https://api.anthropic.com",
            ),
        },
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(
                provider_name="default",
                aliases=("opus", "team-pro"),
            ),
            "claude-sonnet-4-6": ModelEndpoint(
                provider_name="default",
                aliases=("sonnet",),
            ),
            # Include a non-claude alias to exercise the synthetic-
            # id path (the alias ``minimax`` is rewritten to
            # ``minimax-non-anthropic`` so Claude Code's picker
            # filter keeps it).
            "MiniMax-M3": ModelEndpoint(
                provider_name="default",
                aliases=("minimax",),
            ),
        },
        default_model="claude-opus-4-8",
    )
    config_dir = write_profile_config(p, base_dir=base)
    settings = json.loads(
        (config_dir / "settings.json").read_text(encoding="utf-8")
    )
    # Aliases that don't contain ``claude`` / ``anthropic`` get the
    # ``-non-anthropic`` suffix; claude aliases that DO contain
    # ``claude`` would be kept verbatim — but ``opus``,
    # ``team-pro``, ``sonnet`` and ``minimax`` don't contain
    # ``claude`` so all four get the suffix.
    assert settings["availableModels"] == sorted([
        "minimax-non-anthropic",
        "opus-non-anthropic",
        "sonnet-non-anthropic",
        "team-pro-non-anthropic",
    ])
    # Real upstream IDs are NEVER exposed — those go through
    # ``/v1/models`` as the ``id`` field (which the user types
    # the bare upstream name into). The picker only lists aliases.
    assert "claude-opus-4-8" not in settings["availableModels"]
    assert "claude-sonnet-4-6" not in settings["availableModels"]
    assert "MiniMax-M3" not in settings["availableModels"]
    print("✓ write_profile_config exposes aliases in availableModels with synthetic-id format")


def test_write_profile_config_omits_available_models_when_no_aliases():
    """Endpoints with no aliases contribute nothing; if *no* endpoint has
    aliases, the ``availableModels`` key is omitted from the file
    entirely."""
    from src.profiles import ClaudeProfile, ModelEndpoint, Provider, write_profile_config

    base = _tmp_dir()
    p = ClaudeProfile(
        name="Demo",
        slug="demo",
        providers={
            "default": Provider(
                name="default", url="https://api.example.com",
            ),
        },
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(provider_name="default"),
            "claude-haiku-4-5": ModelEndpoint(provider_name="default"),
        },
    )
    config_dir = write_profile_config(p, base_dir=base)
    settings = json.loads(
        (config_dir / "settings.json").read_text(encoding="utf-8")
    )
    # Real names are *not* exposed; no aliases → key absent.
    assert "availableModels" not in settings
    print("✓ write_profile_config omits availableModels when no aliases exist")


def test_write_profile_config_writes_agent_markdown_with_frontmatter():
    from src.profiles import ClaudeProfile, Subagent, write_profile_config

    base = _tmp_dir()
    p = ClaudeProfile(
        name="Demo",
        slug="demo",
        model_endpoints={"claude-opus-4-8": "https://api.example.com"},
        subagents=[
            Subagent(
                name="reviewer",
                description="Reviews code carefully",
                system_prompt="You review code carefully.",
                tools=["Read", "Grep"],
                model="claude-sonnet-4-6",
            ),
        ],
    )
    config_dir = write_profile_config(p, base_dir=base)
    agent_file = config_dir / "agents" / "reviewer.md"
    assert agent_file.is_file()
    body = agent_file.read_text(encoding="utf-8")
    # Frontmatter present and well-formed.
    assert body.startswith("---\n")
    assert "\n---\n" in body
    frontmatter = body.split("\n---\n", 1)[0]
    assert 'name: "reviewer"' in frontmatter
    assert 'description: "Reviews code carefully"' in frontmatter
    assert "tools: [Read, Grep]" in frontmatter
    assert 'model: "claude-sonnet-4-6"' in frontmatter
    assert body.endswith("You review code carefully.\n")
    print("✓ write_profile_config renders agents/<name>.md with YAML frontmatter")


def test_write_profile_config_prunes_stale_agents():
    from src.profiles import ClaudeProfile, Subagent, write_profile_config

    base = _tmp_dir()
    # First write with two agents.
    p = ClaudeProfile(
        name="Demo",
        slug="demo",
        model_endpoints={},
        subagents=[
            Subagent(name="a", description="", system_prompt="A"),
            Subagent(name="b", description="", system_prompt="B"),
        ],
    )
    config_dir = write_profile_config(p, base_dir=base)
    assert (config_dir / "agents" / "a.md").is_file()
    assert (config_dir / "agents" / "b.md").is_file()
    # Now remove "b" and re-write — the file should be gone.
    p.subagents = [p.subagents[0]]
    write_profile_config(p, base_dir=base)
    assert (config_dir / "agents" / "a.md").is_file()
    assert not (config_dir / "agents" / "b.md").exists()
    print("✓ write_profile_config prunes stale agent files on re-write")


# ---- env builder ----------------------------------------------------------


def test_env_builder_includes_infra_vars():
    from src.profiles import ClaudeProfile, build_claude_session_env

    # ``default_model`` moved to ``claude_settings["model"]``.
    p = ClaudeProfile(
        name="P",
        slug="p",
        model_endpoints={},
        claude_settings={"model": "claude-opus-4-8"},
    )
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/tmp/cfg",
        api_key="sk-real",
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:3001"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg"
    assert env["ANTHROPIC_API_KEY"] == "sk-real"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert env["TERM"] == "xterm-256color"
    print("✓ env_builder sets all infra vars from profile + proxy URL")


def test_env_builder_session_overrides_profile_overrides_base():
    from src.profiles import ClaudeProfile, build_claude_session_env

    p = ClaudeProfile(
        name="P",
        slug="p",
        model_endpoints={},
        env_vars={"FOO": "profile", "BAR": "profile"},
    )
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/tmp/cfg",
        api_key="sk",
        session_env={"FOO": "session", "BAZ": "session"},
    )
    assert env["FOO"] == "session", "session must override profile"
    assert env["BAR"] == "profile", "profile must remain when session doesn't override"
    assert env["BAZ"] == "session", "session-only vars must pass through"
    print("✓ env_builder session > profile precedence holds")


def test_env_builder_warns_when_clobbered_and_re_applies():
    from src.profiles import ClaudeProfile, build_claude_session_env

    p = ClaudeProfile(
        name="P",
        slug="p",
        model_endpoints={},
        env_vars={"ANTHROPIC_BASE_URL": "http://malicious.example"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        env = build_claude_session_env(
            profile=p,
            proxy_base_url="http://127.0.0.1:3001",
            config_dir="/tmp/cfg",
            api_key="sk",
        )
    # The infrastructure var must have been re-applied despite the
    # profile's attempt to set it.
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:3001"
    # And a UserWarning was emitted.
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert any(
        "ANTHROPIC_BASE_URL" in str(w.message) for w in user_warnings
    ), f"expected UserWarning about BASE_URL, got {[str(w.message) for w in user_warnings]}"
    print("✓ env_builder warns + re-applies infra vars when clobbered")


def test_env_builder_starts_from_os_environ_copy():
    """PATH and HOME must survive so the child can find its CLI / dotfiles."""
    from src.profiles import ClaudeProfile, build_claude_session_env

    p = ClaudeProfile(name="P", slug="p", model_endpoints={})
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/tmp/cfg",
        api_key="sk",
    )
    # PATH is set in os.environ during pytest's normal startup — it
    # should round-trip through the builder.
    assert env.get("PATH") == os.environ.get("PATH")
    print("✓ env_builder starts from os.environ.copy() (PATH survives)")


def test_env_builder_sentinel_when_no_key():
    from src.profiles import ClaudeProfile, SENTINEL_API_KEY, build_claude_session_env

    p = ClaudeProfile(name="P", slug="p", model_endpoints={})
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/tmp/cfg",
        api_key=None,
    )
    assert env["ANTHROPIC_API_KEY"] == SENTINEL_API_KEY
    print("✓ env_builder uses labeled sentinel when no key configured")


def test_resolve_api_key_resolves_alias_to_canonical_provider():
    """``claude_settings["model"]`` may be a user-friendly alias
    (``"minimax"``) rather than the canonical upstream name
    (``"MiniMax-M3"``). ``resolve_api_key`` must walk the
    endpoints' ``aliases`` to find the right provider's credential.
    Otherwise the session launches with the sentinel
    ``sk-proxy-injected`` and upstream returns *"Invalid API key"*.
    """
    from src.profiles import (
        ClaudeProfile,
        ModelEndpoint,
        Provider,
        resolve_api_key,
    )

    captured: dict = {}

    class _FakeCredentialStore:
        def get(self, ref: str) -> str | None:
            captured["ref"] = ref
            return "sk-real-key-12345" if ref == "minimax-key" else None

    p = ClaudeProfile(
        name="AliasTest",
        slug="alias-test",
        providers={
            "minimax": Provider(
                name="minimax",
                url="https://api.minimax.io/anthropic",
                key_ref="minimax-key",
            ),
        },
        model_endpoints={
            "MiniMax-M3": ModelEndpoint(
                provider_name="minimax",
                aliases=("minimax",),
            ),
        },
        claude_settings={"model": "minimax"},  # alias, not canonical
    )

    key = resolve_api_key(p, _FakeCredentialStore())
    assert key == "sk-real-key-12345", (
        f"resolve_api_key must walk aliases to find the canonical "
        f"endpoint; got {key!r}"
    )
    assert captured == {"ref": "minimax-key"}, (
        f"expected credential lookup against key_ref 'minimax-key', "
        f"got {captured!r}"
    )
    print("✓ resolve_api_key walks aliases to resolve the right credential")


def test_resolve_api_key_returns_none_when_no_match():
    """If neither the canonical lookup nor the alias walk finds
    a match, ``resolve_api_key`` returns ``None`` so the
    env_builder substitutes the labeled sentinel — never raises.
    """
    from src.profiles import (
        ClaudeProfile,
        ModelEndpoint,
        resolve_api_key,
    )

    class _EmptyStore:
        def get(self, ref: str) -> str | None:
            return None

    p = ClaudeProfile(
        name="NoMatch",
        slug="no-match",
        providers={},
        model_endpoints={
            "MiniMax-M3": ModelEndpoint(
                provider_name="minimax", aliases=("minimax",)
            ),
        },
        claude_settings={"model": "unknown-alias"},
    )
    assert resolve_api_key(p, _EmptyStore()) is None
    print("✓ resolve_api_key returns None when no endpoint matches")


# ---- TerminalSessionView env/cwd seam ------------------------------------


def test_terminal_session_view_forwards_env_to_pty():
    """A session with ``env={...}`` must surface those vars to the child."""
    if not hasattr(os, "fork"):
        print("⚠ POSIX-only test skipped on non-POSIX")
        return
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(
        argv=["/bin/sh", "-c", "echo $UNIQ_TEST_VAR"],
        env={**os.environ, "UNIQ_TEST_VAR": "hello-from-env"},
        parent=None,
    )
    view.start()
    # Wait for the echo output to land in the pyte buffer.
    deadline_ms = 1500
    step_ms = 50
    waited = 0
    found = False
    while waited < deadline_ms:
        _spin(step_ms)
        waited += step_ms
        screen = view._screen
        if screen is None:
            continue
        text = "".join(
            screen.buffer[y][x].data
            for y in range(screen.lines)
            for x in range(screen.columns)
        )
        if "hello-from-env" in text:
            found = True
            break
    view.shutdown()
    assert found, "child did not see UNIQ_TEST_VAR env var"
    print("✓ TerminalSessionView forwards env to PTY child")


def test_terminal_session_view_forwards_cwd_to_pty():
    """A session with ``cwd=...`` must chdir the child to that path."""
    if not hasattr(os, "fork"):
        print("⚠ POSIX-only test skipped on non-POSIX")
        return
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    tmp = _tmp_dir()
    view = TerminalSessionView(
        argv=["/bin/sh", "-c", "pwd"],
        env=os.environ.copy(),
        cwd=str(tmp),
        parent=None,
    )
    view.start()
    deadline_ms = 1500
    step_ms = 50
    waited = 0
    found = False
    expected_tail = tmp.name  # basename of the tmpdir
    while waited < deadline_ms:
        _spin(step_ms)
        waited += step_ms
        screen = view._screen
        if screen is None:
            continue
        text = "".join(
            screen.buffer[y][x].data
            for y in range(screen.lines)
            for x in range(screen.columns)
        )
        if expected_tail in text:
            found = True
            break
    view.shutdown()
    assert found, f"child did not chdir to {tmp}"
    print("✓ TerminalSessionView forwards cwd to PTY child")


def test_terminal_session_view_title_for_claude_argv():
    """Tab title is the basename of argv[0] — so ``["claude"]`` → ``"claude"``."""
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["claude"], parent=None)
    assert view.title() == "claude"
    print("✓ TerminalSessionView title() returns basename of argv[0]")


# ---- ClaudeContainerView --------------------------------------------------


def _container_with_proxy_callbacks(
    *, running: bool = False, base_url: str = "", session_cwd: str | None = None
):
    """Build a ClaudeContainerView with stub proxy callbacks.

    ``session_cwd`` (when non-None) is installed as the global
    default session CWD via a one-shot ``set_global_settings_loader``.
    Default session CWD moved from per-profile
    (``profile.session_cwd``) to the global settings store as part
    of the menu restructure; tests that need to drive it install
    a stub loader here.
    """
    from src.ui.claude.claude_container_view import ClaudeContainerView
    from src.profiles.global_settings import GlobalSettings

    cv = ClaudeContainerView()
    cv._suppress_dialogs = True  # headless: skip modal QMessageBox
    cv.set_proxy_callbacks(
        is_proxy_running=lambda: running,
        get_proxy_base_url=lambda: base_url,
        request_proxy_start=lambda: None,
    )
    if session_cwd is not None:
        cv.set_global_settings_loader(
            lambda: GlobalSettings(session_cwd=session_cwd)
        )
    return cv


def test_claude_container_blocks_when_no_claude_binary(monkeypatch=None):
    _qapp()
    cv = _container_with_proxy_callbacks(running=True)
    # Force shutil.which("claude") to return None.
    import src.ui.claude.claude_container_view as cvm

    original = cvm.shutil.which
    cvm.shutil.which = lambda name: None if name == "claude" else original(name)
    try:
        view = cv.new_session()
        assert view is None
    finally:
        cvm.shutil.which = original
    print("✓ ClaudeContainerView refuses to launch when 'claude' not on PATH")


def test_claude_container_queues_when_proxy_not_running():
    _qapp()
    requested = []
    cv = _container_with_proxy_callbacks(running=False)
    cv.proxy_requested.connect(lambda: requested.append(True))
    # No profile configured → should short-circuit before proxy start.
    # Force a profile so we exercise the proxy-request path.
    from src.profiles import ClaudeProfile

    p = ClaudeProfile(name="X", slug="x", model_endpoints={})
    view = cv.new_session(profile=p)
    # Proxy not running → launch deferred → returns None and emits signal.
    assert view is None
    assert requested, "proxy_requested signal was not emitted"
    print("✓ ClaudeContainerView defers launch + asks main window to start proxy")


def test_claude_container_flush_pending_launches():
    _qapp()
    from src.profiles import ClaudeProfile

    # Use a path with a known basename so we can detect it in the PTY output.
    tmp = _tmp_dir()
    p = ClaudeProfile(
        name="X",
        slug="x",
        model_endpoints={},
        # Docker is off by default for tests — the test environment
        # has no docker binary, so a default-ON profile would route
        # through :class:`DockerSession` and never reach the shim.
        run_in_container=False,
    )
    # Patch shutil.which to find our fake "claude" wrapper. We use
    # /bin/sh -c "<check>" as the wrapper — the existing
    # new_session() uses argv=["claude"] verbatim, so we need an
    # actual executable named "claude" on PATH. Simplest: a shim
    # in a temp dir on PATH.
    import src.ui.claude.claude_container_view as cvm

    shim_dir = _tmp_dir()
    shim = shim_dir / "claude"
    # The shim prints the cwd so we can verify env/cwd were wired.
    shim.write_text(
        "#!/bin/sh\n"
        "echo \"claude-shim-cwd=$(pwd)\"\n"
        "echo \"claude-shim-base=$ANTHROPIC_BASE_URL\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{shim_dir}:{saved_path}"
    try:
        cv = _container_with_proxy_callbacks(
            running=False,
            base_url="http://127.0.0.1:3001",
            session_cwd=str(tmp),
        )
        cv.new_session(profile=p)  # queues
        # Now flip the proxy to "running" and flush.
        cv._is_proxy_running = lambda: True
        view = cv.flush_pending_launch()
        assert view is not None, "flush_pending_launch didn't return a view"
        # Spin until the shim exits and the buffer captures our marker.
        deadline_ms = 2000
        step_ms = 50
        waited = 0
        found_cwd = False
        found_base = False
        while waited < deadline_ms and not (found_cwd and found_base):
            _spin(step_ms)
            waited += step_ms
            screen = view._screen
            if screen is None:
                continue
            text = "".join(
                screen.buffer[y][x].data
                for y in range(screen.lines)
                for x in range(screen.columns)
            )
            if f"claude-shim-cwd={tmp}" in text:
                found_cwd = True
            if "claude-shim-base=http://127.0.0.1:3001" in text:
                found_base = True
        view.shutdown()
        assert found_cwd, f"session did not chdir to {tmp}"
        assert found_base, "session did not see ANTHROPIC_BASE_URL"
    finally:
        os.environ["PATH"] = saved_path
    print("✓ ClaudeContainerView.flush_pending_launch wires env + cwd correctly")


def test_claude_container_info_label_stays_alive():
    """Regression: ``info_label`` C++ object must outlive ``_build_ui``.

    Previously the toolbar in ``_build_ui`` was a local variable and
    was never added to the layout, so the QWidget (and its
    ``info_label`` child) was garbage-collected as soon as
    ``_build_ui`` returned. The next call to ``_update_info_label``
    then raised ``RuntimeError: Internal C++ object already deleted``
    — which propagated up out of ``_launch_session`` *before*
    ``view.start()`` ran, so the PTY never forked and the new
    session tab stayed blank.
    """
    from PySide6.QtWidgets import QWidget

    _qapp()
    cv = _container_with_proxy_callbacks(running=True)

    # The toolbar is an instance attribute, not a local variable.
    assert hasattr(cv, "_toolbar"), (
        "_toolbar should be an instance attribute so it survives _build_ui"
    )
    assert isinstance(cv._toolbar, QWidget)

    # The info_label's parent must be the live toolbar (not the
    # already-deleted C++ object of a former local variable).
    assert cv.info_label.parent() is cv._toolbar

    # The toolbar is in the container's layout (so the label is
    # actually visible to the user, not just alive in memory).
    layout = cv.layout()
    in_layout = any(
        layout.itemAt(i).widget() is cv._toolbar
        for i in range(layout.count())
    )
    assert in_layout, "toolbar should be added to the container layout"

    # The actual call that used to crash.
    cv._update_info_label()
    assert cv.info_label.text() == "No sessions yet — click '+ New Session' to start."

    # And with a fake session count, the label updates without error.
    cv._sessions = ["fake"]  # type: ignore[assignment]
    cv._update_info_label()
    assert cv.info_label.text() == "1 session open"

    cv._sessions = ["s1", "s2", "s3"]  # type: ignore[assignment]
    cv._update_info_label()
    assert cv.info_label.text() == "3 sessions open"

    print("✓ ClaudeContainerView._update_info_label does not crash after _build_ui")


# ---- Runner --------------------------------------------------------------


def main():
    tests = [
        test_slugify_basic,
        test_slugify_collapses_punctuation,
        test_slugify_empty_falls_back,
        test_credential_store_roundtrip,
        test_credential_store_chmod_600,
        test_credential_store_corrupt_file_treated_as_empty,
        test_profile_store_roundtrip,
        test_profile_store_migration_seeds_default_from_legacy_config,
        test_profile_store_migration_idempotent,
        test_profile_store_upsert_replaces_existing,
        test_profile_store_delete_promotes_remaining_to_active,
        test_profile_store_dedups_duplicate_slugs,
        test_profile_store_chmod_600,
        test_write_profile_config_writes_settings_json,
        test_write_profile_config_exposes_aliases_in_available_models,
        test_write_profile_config_omits_available_models_when_no_aliases,
        test_write_profile_config_writes_agent_markdown_with_frontmatter,
        test_write_profile_config_prunes_stale_agents,
        test_env_builder_includes_infra_vars,
        test_env_builder_session_overrides_profile_overrides_base,
        test_env_builder_warns_when_clobbered_and_re_applies,
        test_env_builder_starts_from_os_environ_copy,
        test_env_builder_sentinel_when_no_key,
        test_terminal_session_view_forwards_env_to_pty,
        test_terminal_session_view_forwards_cwd_to_pty,
        test_terminal_session_view_title_for_claude_argv,
        test_claude_container_blocks_when_no_claude_binary,
        test_claude_container_queues_when_proxy_not_running,
        test_claude_container_flush_pending_launches,
        test_claude_container_info_label_stays_alive,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n❌ {failed} test(s) failed")
        sys.exit(1)
    print(f"\n✅ All {len(tests)} Claude tests passed!")


if __name__ == "__main__":
    main()