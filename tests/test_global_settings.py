"""Tests for the global settings dialog + backing store.

Covers:

- :class:`GlobalSettings` round-trip via :class:`GlobalSettingsStore`
  (chmod 600 on POSIX, atomic write, defaults on missing file).
- :func:`sync_profile_claude_md` correctly prepends the global
  ``~/.claude/CLAUDE.md`` content into the profile-scoped copy,
  including the marker comment.
- :func:`_rewrite_proxy_for_container` rewrites the loopback URL
  to ``host.docker.internal`` and preserves the port.

These run without a QApplication — the global settings store and
``docker_claude_md`` / ``env_builder`` helpers are pure Python.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Tests run without Qt; explicit platform hint so any accidental
# Qt import doesn't block.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_global_settings_roundtrip(tmp_path: Path) -> None:
    from src.profiles.global_settings import (
        GlobalSettings,
        GlobalSettingsStore,
    )

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    s = GlobalSettings(
        bind_address="0.0.0.0",
        port=4001,
        config_dir="/etc/claude",
        session_cwd="/home/u",
        providers={
            "default": {"url": "http://api.example.com"},
            "work": {
                "url": "http://work.example.com",
                "key_ref": "work-key",
                "available_models": ["claude-opus-4-8"],
            },
        },
    )
    store.save(s)
    loaded = store.load()
    assert loaded.bind_address == "0.0.0.0"
    assert loaded.port == 4001
    assert loaded.config_dir == "/etc/claude"
    assert loaded.session_cwd == "/home/u"
    assert "default" in loaded.providers
    assert loaded.providers["work"].get("key_ref") == "work-key"
    print("✓ GlobalSettings round-trip preserves all fields")


def test_global_settings_missing_file_returns_defaults(tmp_path: Path) -> None:
    from src.profiles.global_settings import (
        GlobalSettingsStore,
    )

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    s = store.load()
    assert s.bind_address == "127.0.0.1"
    assert s.port == 3001
    assert s.providers == {}
    print("✓ GlobalSettings missing-file defaults are stable")


def test_global_settings_atomic_write_chmod(tmp_path: Path) -> None:
    """Atomic write leaves no .tmp files behind and chmods 0o600."""
    from src.profiles.global_settings import (
        GlobalSettings,
        GlobalSettingsStore,
    )

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    store.save(GlobalSettings(bind_address="0.0.0.0"))
    assert (tmp_path / "claude_global.json").is_file()
    if sys.platform != "win32":
        mode = (tmp_path / "claude_global.json").stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    leftovers = list(tmp_path.glob("claude_global_*.tmp"))
    assert not leftovers, f"tmp files left behind: {leftovers}"
    print("✓ GlobalSettings atomic write is clean")


def test_global_settings_dropped_defaults_compact_file(tmp_path: Path) -> None:
    """Saving default GlobalSettings yields a near-empty JSON file."""
    from src.profiles.global_settings import (
        GlobalSettings,
        GlobalSettingsStore,
    )

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    store.save(GlobalSettings())  # all defaults
    raw = json.loads(
        (tmp_path / "claude_global.json").read_text(encoding="utf-8")
    )
    # bind_address / port match defaults — they should be dropped
    assert "bind_address" not in raw
    assert "port" not in raw
    assert raw.get("schema_version") == 1
    print("✓ GlobalSettings drops default fields for compact file")


def test_sync_profile_claude_md_prepends_global(tmp_path: Path, monkeypatch) -> None:
    """Global content is prepended into the profile-scoped CLAUDE.md."""
    from src.profiles import ClaudeProfile
    from src.profiles.global_settings import GLOBAL_CLAUDE_MD_PATH
    from src.ui.claude import docker_claude_md
    from src.ui.claude.docker_claude_md import sync_profile_claude_md

    # Override the global CLAUDE.md path for the duration of this test.
    global_path = tmp_path / "global_claude.md"
    global_path.write_text("# Global rule: prefer concise answers.\n", encoding="utf-8")
    monkeypatch.setattr(docker_claude_md, "GLOBAL_CLAUDE_MD_PATH", global_path)

    # Override the profile base dir to a tmp location.
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    monkeypatch.setattr(
        docker_claude_md, "profile_config_base_dir", lambda: profiles_root
    )

    p = ClaudeProfile(name="P", slug="p")
    out = sync_profile_claude_md(p)
    assert out == profiles_root / "p" / ".claude" / "CLAUDE.md"
    text = out.read_text(encoding="utf-8")
    assert "Global rule: prefer concise answers." in text
    assert "auto-prepended from" in text
    assert "End global section" in text
    print("✓ sync_profile_claude_md prepends global with marker")


def test_sync_profile_claude_md_no_global(tmp_path: Path, monkeypatch) -> None:
    """Missing global file produces a profile CLAUDE.md with no marker."""
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_claude_md
    from src.ui.claude.docker_claude_md import sync_profile_claude_md

    monkeypatch.setattr(
        docker_claude_md,
        "GLOBAL_CLAUDE_MD_PATH",
        tmp_path / "does_not_exist.md",
    )
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    monkeypatch.setattr(
        docker_claude_md, "profile_config_base_dir", lambda: profiles_root
    )
    p = ClaudeProfile(name="P", slug="p")
    out = sync_profile_claude_md(p)
    text = out.read_text(encoding="utf-8")
    assert "Global CLAUDE.md" not in text
    print("✓ sync_profile_claude_md handles missing global file")


def test_rewrite_proxy_for_container_basic() -> None:
    from src.profiles.env_builder import _rewrite_proxy_for_container

    assert (
        _rewrite_proxy_for_container("http://127.0.0.1:3001")
        == "http://host.docker.internal:3001"
    )
    assert (
        _rewrite_proxy_for_container("http://localhost:8080/path")
        == "http://host.docker.internal:8080/path"
    )
    print("✓ _rewrite_proxy_for_container rewrites hostname, preserves port+path")


def test_rewrite_proxy_for_container_passthrough_unparseable() -> None:
    from src.profiles.env_builder import _rewrite_proxy_for_container

    # Unparseable URL passes through untouched.
    assert _rewrite_proxy_for_container("not-a-url") == "not-a-url"
    print("✓ _rewrite_proxy_for_container leaves unparseable URLs alone")


def test_env_builder_container_aware_rewrites() -> None:
    from src.profiles import ClaudeProfile
    from src.profiles.env_builder import build_claude_session_env

    p = ClaudeProfile(
        name="P",
        slug="p",
        claude_settings={"model": "claude-opus-4-8"},
    )
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/host/cfg",
        api_key="sk-test",
        container_aware=True,
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:3001"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/claude/.claude"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test", (
        f"expected auth token set in container mode, "
        f"got {env.get('ANTHROPIC_AUTH_TOKEN')!r}"
    )
    print("✓ env_builder container_aware rewrites infra vars")


def test_env_builder_no_container_keeps_host_values() -> None:
    from src.profiles import ClaudeProfile
    from src.profiles.env_builder import build_claude_session_env

    p = ClaudeProfile(name="P", slug="p")
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/host/cfg",
        api_key="sk-test",
        container_aware=False,
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:3001"
    assert env["CLAUDE_CONFIG_DIR"] == "/host/cfg"
    print("✓ env_builder non-container leaves host values intact")


def test_env_builder_sets_auth_token_when_no_api_key() -> None:
    """When ``api_key`` is ``None`` both ``ANTHROPIC_API_KEY`` and
    ``ANTHROPIC_AUTH_TOKEN`` fall back to the labelled sentinel — Claude
    Code then sees ``Authorization: Bearer sk-proxy-injected`` instead
    of an empty/missing header, so it doesn't refuse to launch.
    """
    from src.profiles import ClaudeProfile, SENTINEL_API_KEY, build_claude_session_env

    p = ClaudeProfile(name="P", slug="p")
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/host/cfg",
        api_key=None,
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == SENTINEL_API_KEY, (
        f"expected sentinel fallback for ANTHROPIC_AUTH_TOKEN, "
        f"got {env['ANTHROPIC_AUTH_TOKEN']!r}"
    )
    assert env["ANTHROPIC_API_KEY"] == SENTINEL_API_KEY, (
        "API key fallback should match auth token fallback"
    )
    print("✓ env_builder falls back to sentinel for ANTHROPIC_AUTH_TOKEN")


def test_env_builder_clobber_safety_net_preserves_auth_token() -> None:
    """If a profile / session env tries to set ``ANTHROPIC_AUTH_TOKEN``
    to an attacker-controlled value, the safety-net re-apply block must
    restore the resolved credential and emit a ``UserWarning``.

    Regression guard: the env_builder was setting
    ``ANTHROPIC_API_KEY`` in the safety-net block but not
    ``ANTHROPIC_AUTH_TOKEN`` — so a profile could clobber the auth
    token even though it's in ``INFRA_ENV_VARS``.
    """
    import warnings

    from src.profiles import ClaudeProfile, build_claude_session_env

    p = ClaudeProfile(
        name="P",
        slug="p",
        env_vars={"ANTHROPIC_AUTH_TOKEN": "evil-token"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        env = build_claude_session_env(
            profile=p,
            proxy_base_url="http://127.0.0.1:3001",
            config_dir="/host/cfg",
            api_key="sk-real",
        )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-real", (
        f"safety-net must restore the resolved auth token, "
        f"got {env['ANTHROPIC_AUTH_TOKEN']!r}"
    )
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert any(
        "ANTHROPIC_AUTH_TOKEN" in str(w.message) for w in user_warnings
    ), (
        f"expected UserWarning about ANTHROPIC_AUTH_TOKEN clobber, "
        f"got {[str(w.message) for w in user_warnings]}"
    )
    print("✓ env_builder safety-net restores ANTHROPIC_AUTH_TOKEN on clobber")


def test_env_builder_container_aware_sets_auth_token() -> None:
    """The container-aware path also sets ``ANTHROPIC_AUTH_TOKEN`` —
    Claude Code inside the container needs it for Bearer auth on
    gateways like MiniMax."""
    from src.profiles import ClaudeProfile, build_claude_session_env

    p = ClaudeProfile(
        name="P",
        slug="p",
        claude_settings={"model": "claude-opus-4-8"},
    )
    env = build_claude_session_env(
        profile=p,
        proxy_base_url="http://127.0.0.1:3001",
        config_dir="/host/cfg",
        api_key="sk-test",
        container_aware=True,
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["ANTHROPIC_BASE_URL"] == "http://host.docker.internal:3001"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/claude/.claude"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    print("✓ env_builder container_aware sets ANTHROPIC_AUTH_TOKEN alongside other infra vars")


def test_global_settings_dialog_opens(tmp_path: Path) -> None:
    """Constructing the dialog must not raise — regression test for the
    ``textInteractionFlags() | 1`` bitwise-or TypeError that crashed
    the CLAUDE.md tab on dialog open.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.profiles.global_settings import GlobalSettingsStore
    from src.ui.claude.global_settings import GlobalSettingsDialog

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    dlg = GlobalSettingsDialog(store)
    # The CLAUDE.md tab is the one that previously raised.
    titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert "CLAUDE.md" in titles
    # And the path label must be selectable (TextInteractionFlag).
    claudemd_idx = titles.index("CLAUDE.md")
    page = dlg.tabs.widget(claudemd_idx)
    labels = page.findChildren(type(page).__mro__[0])  # any widget tree walk
    # Light-weight sanity check: the path label's textInteractionFlag
    # is non-empty (i.e. selection is enabled).
    from PySide6.QtWidgets import QLabel

    path_labels = page.findChildren(QLabel)
    selectable = [
        lbl for lbl in path_labels
        if bool(lbl.textInteractionFlags())
        and "Path:" in lbl.text()
    ]
    assert selectable, "Path label missing text-selection flag"
    print("✓ GlobalSettingsDialog opens without raising (CLAUDE.md tab fix)")


def test_profile_editor_endpoints_table_has_no_api_key_column(tmp_path: Path) -> None:
    """The profile Model Configuration tab deliberately has no
    API Key column — credentials are owned by Claude > Settings
    (Providers + API Keys) and inherited via the provider's
    ``key_ref`` at request time. Surfacing credentials in the
    profile editor would invite per-profile overrides that drift
    out of sync with the global registry.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.profiles.global_settings import GlobalSettingsStore
    from src.profiles.credential_store import CredentialStore
    from src.profiles.models import ClaudeProfile, ModelEndpoint
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    gs_store = GlobalSettingsStore(tmp_path / "claude_global.json")
    cs = CredentialStore(tmp_path / "credentials.json")
    p = ClaudeProfile(
        name="P",
        slug="p",
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(
                provider_name="default", aliases=("opus",)
            ),
        },
    )
    dlg = ProfileEditorDialog(
        p, credential_store=cs, global_settings_store=gs_store
    )
    headers = [
        dlg.endpoints_table.horizontalHeaderItem(i).text()
        for i in range(dlg.endpoints_table.columnCount())
    ]
    assert headers == ["Aliases", "Provider", "Model"], headers
    assert "API Key" not in headers, "API Key column must not appear in profile editor"
    print("✓ Profile Model Configuration has no API Key column (credentials are global)")


def test_profile_editor_uses_global_settings_loader(tmp_path: Path, monkeypatch) -> None:
    """Model Configuration Provider combo is populated from the
    live global provider registry via the loader the main window
    installs, not from the on-disk file (which may not exist yet
    on first launch). Construct the dialog with a loader that
    returns providers from an in-memory store and verify they
    appear in the form's provider combo.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.profiles.global_settings import GlobalSettings
    from src.profiles.credential_store import CredentialStore
    from src.profiles.models import ClaudeProfile, ModelEndpoint
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    settings = GlobalSettings(providers={
        "default": {"url": "http://api.example.com", "key_ref": "default"},
        "anthropic": {"url": "https://api.anthropic.com", "key_ref": "anthropic"},
        "work": {"url": "https://work.example.com", "key_ref": "work"},
    })

    cs = CredentialStore(tmp_path / "credentials.json")
    p = ClaudeProfile(
        name="P",
        slug="p",
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(
                provider_name="default", aliases=("opus",)
            ),
        },
    )
    # Loader is callable (not a store). The dialog invokes it on
    # every refresh — install one that returns our in-memory store.
    dlg = ProfileEditorDialog(
        p,
        credential_store=cs,
        global_settings_loader=lambda: settings,
    )

    # Switch to a row so the form is populated.
    dlg.endpoints_table.selectRow(0)

    # The form's provider combo must list every name from the
    # global registry — not just those referenced by an
    # endpoint.
    provider_names = [
        dlg.endpoint_provider_combo.itemText(i)
        for i in range(dlg.endpoint_provider_combo.count())
    ]
    for expected in ("default", "anthropic", "work"):
        assert expected in provider_names, (
            f"Provider {expected!r} missing from form combo: {provider_names}"
        )
    print("✓ Profile editor Provider combo populated from live global loader")


def test_resolve_launch_mode_falls_back_without_docker(monkeypatch) -> None:
    """``run_in_container=True`` + missing docker CLI → local PTY.

    Regression test for the bug where launching a session with
    Docker requested but no docker binary available produced a
    silent ``claude (exited)`` tab. The resolver must report
    ``"pty"`` so the launcher falls back gracefully.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import shutil

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    # Force shutil.which("docker") → None for this test.
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "docker" else f"/usr/bin/{name}")
    from src.profiles import ClaudeProfile
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    p = ClaudeProfile(name="P", slug="p", run_in_container=True)
    assert cv._resolve_launch_mode(p) == "pty"
    # Non-docker profile always uses pty regardless of the binary.
    p2 = ClaudeProfile(name="P2", slug="p2", run_in_container=False)
    assert cv._resolve_launch_mode(p2) == "pty"
    print("✓ _resolve_launch_mode falls back to PTY when docker CLI is absent")


def test_resolve_launch_mode_picks_up_runtime_switch(monkeypatch) -> None:
    """``_resolve_launch_mode`` re-reads the global settings on every
    call, so a runtime switch (docker → podman) takes effect on the
    next launch without restarting the app.

    Regression test for the bug where the launcher reported
    *"Profile 'Default' requested Docker but docker CLI not found
    on PATH; launching locally instead"* even though the user had
    set ``container_runtime: "podman"`` in Claude > Settings. The
    previous implementation cached ``shutil.which("docker")`` on
    the first call, pinning the answer to whatever PATH looked
    like at app start; subsequent runtime switches in global
    settings were silently ignored.

    Now the resolver re-reads the live global settings (via the
    installed ``_load_global_settings`` callback) on every call,
    so changing ``container_runtime`` is picked up immediately.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import shutil

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from src.profiles import ClaudeProfile
    from src.profiles.global_settings import GlobalSettings
    from src.ui.claude.claude_container_view import ClaudeContainerView

    # ``which("docker")`` returns None (docker is missing), but
    # ``which("podman")`` returns a path (podman is on PATH).
    def _fake_which(name: str) -> str | None:
        if name == "docker":
            return None
        if name == "podman":
            return "/usr/bin/podman"
        return f"/usr/bin/{name}"

    monkeypatch.setattr(shutil, "which", _fake_which)

    cv = ClaudeContainerView()
    # Simulate the live global settings loader returning a
    # GlobalSettings with a runtime the user changes between
    # calls. Container is given a fresh mutable holder so we
    # can swap the runtime mid-test.
    runtime_holder = {"value": "docker"}

    class _FakeSettings:
        container_runtime = ""
        # Property reads from the live holder so the test can
        # mutate ``runtime_holder["value"]`` between resolver calls.
        @property
        def container_runtime_value(self):  # pragma: no cover — unused
            return runtime_holder["value"]

    def _fake_settings():
        s = GlobalSettings()
        s.container_runtime = runtime_holder["value"]
        return s

    cv.set_global_settings_loader(_fake_settings)
    profile = ClaudeProfile(name="P", slug="p", run_in_container=True)

    # First call: docker requested, docker missing → pty.
    runtime_holder["value"] = "docker"
    assert cv._resolve_launch_mode(profile) == "pty", (
        "docker missing on PATH should fall back to pty"
    )

    # Second call: user switches to podman in Claude > Settings.
    # Without the cache fix, this would still return "pty" because
    # the first call's result was cached. With the fix, the
    # resolver re-reads the live loader, sees podman, and finds
    # the podman binary on PATH → docker mode.
    runtime_holder["value"] = "podman"
    assert cv._resolve_launch_mode(profile) == "docker", (
        "switching container_runtime to podman should pick up "
        "podman availability on the next call without app restart"
    )
    print("✓ _resolve_launch_mode re-reads runtime from live settings")


def test_global_settings_providers_persist_as_dicts(tmp_path: Path) -> None:
    """Regression: providers added in Claude > Settings > Providers
    must round-trip through the on-disk store as plain dicts, not
    :class:`Provider` dataclass instances. The previous version
    of ``_collect_providers_tab`` created ``Provider`` objects,
    which ``_serialize_providers`` silently dropped because it
    only accepts ``dict`` values — so the dialog saved without
    raising but the next dialog open showed an empty Provider
    combo in the profile editor.
    """
    import copy

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.profiles.global_settings import GlobalSettingsStore
    from src.profiles.credential_store import CredentialStore
    from src.profiles.models import ClaudeProfile
    from src.ui.claude.global_settings import GlobalSettingsDialog
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    gs_store = GlobalSettingsStore(tmp_path / "claude_global.json")
    cs = CredentialStore(tmp_path / "credentials.json")

    # 1. User opens Claude > Settings > Providers, adds a row,
    #    hits OK. The collect helper must store a dict (not a
    #    ``Provider`` instance) so the serializer keeps it.
    gsd = GlobalSettingsDialog(gs_store)
    gsd._append_provider_row(
        "default",
        "http://api.example.com",
        key_ref="default",
        available_models=(),
        is_default=True,
    )
    gsd._append_provider_row(
        "minimax",
        "http://api.minimax.com",
        key_ref=None,
        available_models=(),
        is_default=False,
    )
    gsd._on_accept()

    # 2. On-disk file has both providers as dicts.
    on_disk = gs_store.load()
    assert "default" in on_disk.providers, on_disk.providers
    assert "minimax" in on_disk.providers, on_disk.providers
    for name, prov in on_disk.providers.items():
        assert isinstance(prov, dict), (
            f"Provider {name!r} persisted as {type(prov).__name__}; "
            "the serializer only handles dicts"
        )

    # 3. Profile editor opened with the loader sees both names in
    #    its Provider combo.
    p = ClaudeProfile(name="Default", slug="default")
    ped = ProfileEditorDialog(
        copy.deepcopy(p),
        credential_store=cs,
        global_settings_loader=lambda: gs_store.load(),
    )
    ped.endpoints_table.selectRow(0)
    combo = [
        ped.endpoint_provider_combo.itemText(i)
        for i in range(ped.endpoint_provider_combo.count())
    ]
    assert "default" in combo and "minimax" in combo, combo
    print(
        "✓ Providers added in Claude > Settings > Providers "
        "persist as dicts and surface in Model Configuration"
    )


def test_global_settings_container_fields_roundtrip(tmp_path: Path) -> None:
    """Container runtime / image / proxy-in-container fields
    round-trip through the on-disk store. These power the new
    Container Runtime section of Claude > Settings > General.
    """
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    s = GlobalSettings(
        container_runtime="podman",
        default_claude_image="my-claude:v2",
        proxy_image="my-proxy:latest",
        run_proxy_in_container=True,
    )
    store.save(s)
    loaded = store.load()
    assert loaded.container_runtime == "podman"
    assert loaded.default_claude_image == "my-claude:v2"
    assert loaded.proxy_image == "my-proxy:latest"
    assert loaded.run_proxy_in_container is True
    print("✓ Container runtime + image + proxy-in-container round-trip through store")


def test_global_settings_container_defaults_dropped(tmp_path: Path) -> None:
    """Saving a default-valued GlobalSettings leaves no
    ``container_runtime`` / ``default_claude_image`` /
    ``proxy_image`` / ``run_proxy_in_container`` keys in the
    JSON file — keeps the file diff clean.
    """
    from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore

    store = GlobalSettingsStore(tmp_path / "claude_global.json")
    store.save(GlobalSettings())  # all defaults
    raw = json.loads(
        (tmp_path / "claude_global.json").read_text(encoding="utf-8")
    )
    for key in (
        "container_runtime",
        "default_claude_image",
        "proxy_image",
        "run_proxy_in_container",
    ):
        assert key not in raw, f"{key!r} should be dropped on default values"
    print("✓ Default container settings are dropped for compact JSON")


def test_profile_container_tab_persists_fields(tmp_path: Path) -> None:
    """The profile editor's Container tab persists
    ``run_in_container`` + ``container_image`` + ``container_name``
    to the in-memory profile when the user hits OK. Round-trip
    via :class:`ProfileEditorDialog` mimics the real flow.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import copy

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.profiles.global_settings import GlobalSettingsStore
    from src.profiles.credential_store import CredentialStore
    from src.profiles.models import ClaudeProfile
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    gs_store = GlobalSettingsStore(tmp_path / "claude_global.json")
    cs = CredentialStore(tmp_path / "credentials.json")
    p = ClaudeProfile(name="P", slug="p", run_in_container=False)

    dlg = ProfileEditorDialog(
        copy.deepcopy(p),
        credential_store=cs,
        global_settings_store=gs_store,
    )
    # The Container tab is the new one — verify it exists.
    titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert "Container" in titles, titles

    # Toggle + set custom values, then collect (no re-populate
    # in between — that would reset the checkbox state from the
    # original profile's ``run_in_container``).
    dlg.run_in_container_check.setChecked(True)
    dlg.container_image_edit.setText("my-team-claude:v3")
    dlg.container_name_edit.setText("claude-team-a")
    dlg._collect_container_tab()
    # ``dlg._profile`` is the deep-copied profile the dialog
    # was constructed with — verify the in-memory mutation.
    assert dlg._profile.run_in_container is True
    assert dlg._profile.container_image == "my-team-claude:v3"
    assert dlg._profile.container_name == "claude-team-a"
    print("✓ Profile Container tab persists run_in_container + image + name")


def test_docker_session_runtime_override() -> None:
    """``set_runtime_binary`` updates the binary used by
    ``ensure_container``. Without the override, defaults to
    ``"docker"``.
    """
    from src.ui.claude import docker_session

    docker_session.set_runtime_binary("podman")
    resolved = docker_session._resolve_docker_binary()
    # Whichever binary the test host has; just verify the call
    # respects the override rather than asserting a specific
    # path.
    assert resolved.endswith("podman") or resolved == "", resolved
    docker_session.set_runtime_binary("docker")
    print("✓ docker_session.set_runtime_binary overrides the runtime")


def test_ensure_container_starts_stopped_container(monkeypatch, tmp_path) -> None:
    """``ensure_container`` brings an existing-but-stopped container
    back up before returning.

    Regression test for the "can only create exec sessions on
    running containers: container state improper" error from
    ``docker exec``. The previous behaviour only checked
    ``<runtime> inspect <name>`` exit code — a stopped container
    exited 0, so the helper returned the name and the subsequent
    ``exec`` crashed. The fix inspects ``State.Running`` and runs
    ``<runtime> start`` when the container is stopped.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="Stopped",
        slug="stopped-profile",
        container_image="alpine:latest",
    )

    # Mock ``subprocess.run`` to record the command sequence. Each
    # call returns a ``CompletedProcess``-like object whose
    # ``returncode`` / ``stdout`` is read by ``ensure_container``.
    calls: list[list[str]] = []

    class _FakeResult:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        # First call: bare ``inspect <name>`` (existence check).
        if cmd == ["docker", "inspect", "stopped-profile"]:
            return _FakeResult(returncode=0)
        # Second call: ``inspect --format {{.State.Running}}`` (state
        # check). Return ``false`` so the helper takes the start
        # branch.
        if "--format" in cmd and "State.Running" in " ".join(cmd):
            return _FakeResult(returncode=0, stdout="false")
        # Third call: ``start <name>`` — the fix.
        if cmd[:2] == ["docker", "start"]:
            return _FakeResult(returncode=0)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(docker_session.subprocess, "run", _fake_run)
    # Bypass ``_resolve_docker_binary`` so the test doesn't need a
    # real ``docker`` on PATH.
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")
    # Avoid the side effect of writing a scoped CLAUDE.md into the
    # user's real ``data_dir()`` — the test only exercises the
    # existing-container branch, so the create path doesn't run.
    monkeypatch.setattr(
        docker_session, "sync_profile_claude_md", lambda profile: None
    )

    # Restore the runtime binary override the test above left set
    # before monkeypatching; not strictly needed because
    # ``_resolve_docker_binary`` is patched, but keeps the
    # post-test invariant explicit.
    docker_session.set_runtime_binary("docker")
    try:
        name = docker_session.ensure_container(profile)
        assert name == "stopped-profile", f"expected resolved name, got {name!r}"
        # The critical assertion: a `start` call was issued between
        # the two inspects. Without the fix, this list would have
        # only the two inspects + no `start`.
        start_calls = [c for c in calls if c[:2] == ["docker", "start"]]
        assert len(start_calls) == 1, (
            f"expected exactly one `<runtime> start` call to bring the "
            f"stopped container up, got {len(start_calls)}. "
            f"Full call sequence: {calls!r}"
        )
        # The state check must come *before* the start — i.e. the
        # helper inspected `.State.Running` and confirmed the
        # container wasn't running before starting it. This is what
        # skips the redundant start on already-running containers.
        state_check_idx = next(
            i for i, c in enumerate(calls)
            if "--format" in c and "State.Running" in " ".join(c)
        )
        start_idx = next(i for i, c in enumerate(calls) if c[:2] == ["docker", "start"])
        assert state_check_idx < start_idx, (
            f"state check must precede start, got call sequence: {calls!r}"
        )
    finally:
        pass
    print("✓ ensure_container brings a stopped container back up")


def test_ensure_container_create_overrides_entrypoint(monkeypatch, tmp_path) -> None:
    """``ensure_container`` overrides the entrypoint when creating
    a fresh container.

    Regression test for the "session exits ~4 s after start"
    failure mode. The image's default ``CMD ["claude"]`` would
    otherwise run Claude as PID 1 the moment the container
    starts — but ``podman start`` does not attach stdin, so
    Claude hits its 3-s "no stdin" fallback, switches to
    ``--print`` mode, errors with *"Input must be provided
    either through stdin or as a prompt argument"*, and exits.
    The container then exits with status 1, and our subsequent
    ``exec`` fails with *"container state improper"*.

    The fix passes ``--entrypoint ["sleep", "infinity"]`` to
    ``<runtime> create`` so the container stays alive without
    running Claude. The actual Claude process is launched
    exclusively through ``<runtime> exec -i -t <container> claude``
    from :class:`DockerSession.run`, where the host-side pty
    bridge makes ``isatty(0)`` true and Claude stays in
    interactive mode.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="Fresh",
        slug="fresh-profile",
        container_image="alpine:latest",
    )

    calls: list[list[str]] = []

    class _FakeResult:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        # First call: ``inspect <name>`` → return non-zero so the
        # helper takes the create branch.
        if cmd == ["docker", "inspect", "fresh-profile"]:
            return _FakeResult(returncode=1)
        # Second call: ``create ...`` → return success.
        return _FakeResult(returncode=0)

    monkeypatch.setattr(docker_session.subprocess, "run", _fake_run)
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")
    # Avoid writing CLAUDE.md / profile config dirs into the user's
    # real ``data_dir()``.
    monkeypatch.setattr(
        docker_session, "sync_profile_claude_md", lambda profile: None
    )
    # ``profile_config_base_dir`` resolves through ``QStandardPaths``;
    # patch it to the test's tmp_path so we don't create a host
    # directory under ``~/.local/share/sharkdrown/...``.
    from src.profiles import paths_helpers

    monkeypatch.setattr(
        paths_helpers, "profile_config_base_dir", lambda: tmp_path
    )

    name = docker_session.ensure_container(profile)
    assert name == "fresh-profile"

    # The critical assertion: the create call included
    # ``--entrypoint ["sleep", "infinity"]`` (or equivalent JSON
    # array). Without this override, the container's default CMD
    # would run Claude at start, hit the 3-s stdin timeout, and
    # exit — taking the user's session with it.
    create_calls = [c for c in calls if c[:2] == ["docker", "create"]]
    assert len(create_calls) == 1, (
        f"expected exactly one `<runtime> create` call, got "
        f"{len(create_calls)}. Full call sequence: {calls!r}"
    )
    create_cmd = create_calls[0]
    assert "--entrypoint" in create_cmd, (
        f"create must include --entrypoint to override the "
        f"image's CMD, got: {create_cmd!r}"
    )
    ep_idx = create_cmd.index("--entrypoint")
    ep_value = create_cmd[ep_idx + 1]
    # The entrypoint JSON array must contain ``sleep`` and
    # ``infinity`` so the container stays alive without running
    # Claude at start.
    assert "sleep" in ep_value and "infinity" in ep_value, (
        f"entrypoint must keep the container alive without "
        f"running Claude; got {ep_value!r}"
    )
    print("✓ ensure_container overrides entrypoint to keep container alive")


def test_ensure_container_binds_profile_dir_to_home(monkeypatch, tmp_path) -> None:
    """``ensure_container`` mounts ``<profile>/`` (not ``<profile>/.claude/``)
    onto ``/home/claude`` so SharkDrown's ``settings.json`` / ``agents/`` /
    ``plugins/`` (under ``<profile>/.claude/``) appear at
    ``/home/claude/.claude/`` inside the container — where Claude Code
    reads them.

    Regression guard: the previous layout mounted
    ``<profile>/.claude → /home/claude/.claude``, leaving SharkDrown's
    ``settings.json`` outside the bind-mount. Claude Code inside the
    container saw an empty ``~/.claude/``, tried to mkdir its own
    config dirs, and failed with EACCES because the bind-mount target
    wasn't a writable parent. With the new layout the entire
    ``<profile>/`` is writable from the container's perspective, so
    Claude can create ``~/.claude/`` (== ``<profile>/.claude/`` on the
    host) on demand.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="Mount",
        slug="mount-profile",
        container_image="alpine:latest",
    )

    calls: list[list[str]] = []

    class _FakeResult:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "inspect"]:
            return _FakeResult(returncode=1)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(docker_session.subprocess, "run", _fake_run)
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")
    monkeypatch.setattr(
        docker_session, "sync_profile_claude_md", lambda profile: None
    )
    # ``profile_config_base_dir`` is bound at import time in
    # ``docker_session`` (via ``from … import …``) so we have to
    # patch the *bound* name on ``docker_session`` itself, not on
    # the source module.
    monkeypatch.setattr(
        docker_session, "profile_config_base_dir", lambda: tmp_path
    )

    name = docker_session.ensure_container(profile)
    assert name == "mount-profile"

    create_calls = [c for c in calls if c[:2] == ["docker", "create"]]
    assert len(create_calls) == 1
    create_cmd = create_calls[0]
    # The bind-mount entry must point at ``<profile>/`` (the parent
    # directory) and at ``/home/claude`` (not ``/home/claude/.claude``).
    mounts = [
        arg for arg in create_cmd
        if isinstance(arg, str) and arg.startswith("type=bind")
    ]
    assert len(mounts) == 1, (
        f"expected exactly one bind-mount in create cmd, got: {mounts!r}"
    )
    mount = mounts[0]
    # The source is ``<tmp_path>/<slug>`` — the *parent* of ``.claude``.
    expected_source = str(tmp_path / "mount-profile")
    assert f"source={expected_source}" in mount, (
        f"bind-mount source must be the profile dir, not .claude; "
        f"got {mount!r}"
    )
    assert "target=/home/claude" in mount, (
        f"bind-mount target must be /home/claude (Claude's HOME), "
        f"got {mount!r}"
    )
    # Sanity: the source must NOT include ``.claude`` — that would mean
    # we regressed to the old layout where SharkDrown's settings.json
    # sat outside the bind-mount.
    assert ".claude" not in mount, (
        f"bind-mount source must NOT include .claude; got {mount!r}"
    )
    print("✓ ensure_container binds <profile>/ to /home/claude")


def test_ensure_container_uses_keep_id_userns(monkeypatch, tmp_path) -> None:
    """``ensure_container`` passes ``--userns=keep-id`` so the host
    UID is mapped 1:1 into the container's user namespace.

    Without ``--userns=keep-id``, rootless podman shifts the host's
    UID 1000 (``craig``) into the container's UID 0 (``root``) for
    the bind-mount source. The in-container ``claude`` user (UID
    1000, baked into the image) then sees the host files as owned
    by *root* and can't write to ``~/.claude`` — Claude Code
    surfaces ``EACCES: permission denied, mkdir '/home/claude/...'``
    followed by *"Invalid API key"* (because it never reaches the
    network layer).

    ``--userns=keep-id`` keeps host UID 1000 mapped to container
    UID 1000, so the bind-mount source owner (``craig``, UID 1000)
    appears in the container as UID 1000 = ``claude`` user, making
    the bind-mount writable.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="UIDMap",
        slug="uid-map-profile",
        container_image="alpine:latest",
    )

    calls: list[list[str]] = []

    class _FakeResult:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "inspect"]:
            return _FakeResult(returncode=1)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(docker_session.subprocess, "run", _fake_run)
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")
    monkeypatch.setattr(
        docker_session, "sync_profile_claude_md", lambda profile: None
    )
    monkeypatch.setattr(
        docker_session, "profile_config_base_dir", lambda: tmp_path
    )

    name = docker_session.ensure_container(profile)
    assert name == "uid-map-profile"

    create_calls = [c for c in calls if c[:2] == ["docker", "create"]]
    assert len(create_calls) == 1
    create_cmd = create_calls[0]
    assert "--userns=keep-id" in create_cmd, (
        f"create must include --userns=keep-id so host UIDs map "
        f"1:1 into the container; got {create_cmd!r}"
    )
    # ``--user`` must NOT be hardcoded — the image's ``USER claude``
    # already sets the entrypoint user, and the per-host-UID image
    # build means that user matches the host UID automatically.
    # Hardcoding ``--user 1000:1000`` here would break for any
    # host user whose UID isn't 1000.
    assert "--user" not in create_cmd, (
        f"create must not hardcode --user; the image's USER "
        f"directive already sets the entrypoint user. Got: {create_cmd!r}"
    )
    print("✓ ensure_container uses --userns=keep-id (no hardcoded --user)")


def test_docker_session_uses_tty_for_exec(monkeypatch) -> None:
    """``DockerSession.run()`` spawns ``docker exec -i -t`` so the
    child sees a real TTY.

    Regression test for the "no stdin data received in 3s,
    proceeding without it. If piping from a slow command, redirect
    stdin explicitly longer. Error: Input must be provided either
    through stdin or as a prompt argument when using --print"
    error from the Claude CLI. The CLI's TTY detection sees
    ``isatty(0) == false`` when ``docker exec -i`` is used
    without ``-t`` and switches to ``--print`` mode, which then
    errors out because no prompt argument was passed. The fix
    allocates a host-side pty (``pty.openpty()``) and uses the
    slave fd as the child's stdin/stdout/stderr, plus the
    ``-t`` flag for the container side.

    The mock captures the ``Popen`` call without actually
    spawning the container. We assert the critical invariants
    of the fix: ``-t`` is in the exec command, stdin/stdout/stderr
    are set to the pty slave fd (not PIPE), and the close_fds
    flag is on so the kernel doesn't keep the slave fd open in
    the QThread.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="TTY",
        slug="tty-profile",
        container_image="alpine:latest",
    )

    captured: dict = {}

    class _FakeProcess:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

        def wait(self, timeout=None):
            return 0

    # Pre-create the container so the test doesn't actually shell
    # out to ``docker inspect`` / ``docker create``. Bypass
    # ``ensure_container`` entirely.
    monkeypatch.setattr(
        docker_session, "ensure_container",
        lambda profile: "tty-profile",
    )
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")
    monkeypatch.setattr(docker_session.subprocess, "Popen", _FakeProcess)

    # Bypass the QThread's reader loop — we just want to confirm
    # the Popen invocation, not actually read from a pty.
    monkeypatch.setattr(
        docker_session.DockerSession,
        "_reader_thread_target",
        lambda self: None,
    )

    session = docker_session.DockerSession(
        profile,
        argv=["claude"],
        env={"TERM": "xterm-256color"},
    )
    # ``run()`` is the QThread entry point; call it directly.
    session.run()

    # 1. The exec command included ``-t`` for the container side.
    cmd = captured["args"][0]
    assert "-t" in cmd, (
        f"expected -t flag for container-side TTY, got cmd: {cmd!r}"
    )
    assert "-i" in cmd, f"expected -i flag for stdin, got cmd: {cmd!r}"
    # 2. ``docker exec -i -t -e KEY=VALUE ... <container> <argv...>`` —
    # the prefix is fixed, then env vars come as ``-e KEY=VALUE`` pairs,
    # then the container name, then the trailing argv.
    assert cmd[0] == "docker"
    assert cmd[:3] == ["docker", "exec", "-i"]
    assert "tty-profile" in cmd
    assert cmd[-1] == "claude", f"expected argv appended at end, got cmd: {cmd!r}"
    # The test passed ``env={"TERM": "xterm-256color"}`` so there should
    # be exactly one ``-e`` flag pair between ``-t`` and the container.
    container_idx = cmd.index("tty-profile")
    prefix = cmd[3:container_idx]
    assert prefix[0] == "-t", f"expected -t at cmd[3], got {prefix!r}"
    e_flags = prefix[1:]
    # ``-e KEY=VALUE`` is two list entries, so length must be even.
    assert len(e_flags) % 2 == 0, f"unpaired -e flags: {e_flags!r}"
    pairs = list(zip(e_flags[0::2], e_flags[1::2]))
    assert all(flag == "-e" for flag, _ in pairs), (
        f"all env flags must be -e, got {[flag for flag, _ in pairs]!r}"
    )
    assert ("-e", "TERM=xterm-256color") in pairs, (
        f"expected TERM passed via -e, got pairs: {pairs!r}"
    )

    # 3. stdin/stdout/stderr are set to the pty slave fd, not PIPE.
    # Without the pty bridge, the child's ``isatty(0)`` returns
    # false and the CLI switches to ``--print`` mode.
    kwargs = captured["kwargs"]
    stdin = kwargs["stdin"]
    stdout = kwargs["stdout"]
    stderr = kwargs["stderr"]
    assert stdin is not None and stdin is not docker_session.subprocess.PIPE, (
        f"stdin should be a pty slave fd, not PIPE; got {stdin!r}"
    )
    assert stdout is not None and stdout is not docker_session.subprocess.PIPE, (
        f"stdout should be the pty slave fd, not PIPE; got {stdout!r}"
    )
    assert stderr is not None and stderr is not docker_session.subprocess.PIPE, (
        f"stderr should be the pty slave fd, not PIPE; got {stderr!r}"
    )
    # All three should be the same fd (the pty slave).
    assert stdin == stdout == stderr, (
        f"stdin/stdout/stderr should all be the same pty slave fd, "
        f"got stdin={stdin!r} stdout={stdout!r} stderr={stderr!r}"
    )

    # 4. ``close_fds=True`` so the QThread's own copies of the
    # pty master get GC'd correctly (kernel keeps the slave
    # alive as long as the child has it).
    assert kwargs.get("close_fds") is True, (
        "close_fds=True needed so the parent doesn't leak the "
        "child's pty slave fd across fork"
    )
    print("✓ DockerSession exec uses -t and a pty slave for stdin")


def test_docker_session_passes_env_via_e_flags(monkeypatch) -> None:
    """``DockerSession.run()`` translates ``self._env`` into ``-e KEY=VALUE``
    flags on the ``podman exec`` argv so the inner ``claude`` process
    sees the same env the env_builder assembled.

    Regression test for the bug where the host env dict was passed
    to ``Popen(env=...)`` (which sets the env on the *outer* podman
    process) but podman exec does NOT inherit host env vars by
    default — so the inner ``claude`` saw no ANTHROPIC_BASE_URL,
    no auth token, no CLAUDE_CONFIG_DIR.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="EnvFlags",
        slug="env-flags",
        container_image="alpine:latest",
    )

    captured: dict = {}

    class _FakeProcess:
        def __init__(self, *args, **kwargs):
            captured["args"] = args

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        docker_session, "ensure_container",
        lambda profile: "env-flags",
    )
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")
    monkeypatch.setattr(docker_session.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr(
        docker_session.DockerSession,
        "_reader_thread_target",
        lambda self: None,
    )

    env = {
        "ANTHROPIC_BASE_URL": "http://host.docker.internal:3001",
        "ANTHROPIC_AUTH_TOKEN": "sk-test-token",
        "ANTHROPIC_API_KEY": "sk-test-token",
        "CLAUDE_CONFIG_DIR": "/home/claude/.claude",
        "TERM": "xterm-256color",
    }
    session = docker_session.DockerSession(
        profile,
        argv=["claude", "--foo"],
        env=env,
    )
    session.run()

    cmd = captured["args"][0]
    assert cmd[0] == "docker"
    assert cmd[1] == "exec"
    # ``-i`` and ``-t`` come first (the prefix).
    assert cmd[2] == "-i"
    assert cmd[3] == "-t"
    # ``-e KEY=VALUE`` pairs come between the prefix and the container.
    container_idx = cmd.index("env-flags")
    flag_region = cmd[4:container_idx]
    # Each flag is two entries: ``-e``, ``KEY=VALUE``.
    assert len(flag_region) % 2 == 0, (
        f"flag region must be pairs of (-e, KEY=VALUE), got {flag_region!r}"
    )
    pairs = list(zip(flag_region[0::2], flag_region[1::2]))
    assert all(flag == "-e" for flag, _ in pairs), (
        f"every env flag must be -e, got {[f for f, _ in pairs]!r}"
    )
    # Every key in ``env`` shows up as a ``-e KEY=VALUE`` flag.
    for key, value in env.items():
        assert ("-e", f"{key}={value}") in pairs, (
            f"missing -e flag for {key}={value!r}; pairs: {pairs!r}"
        )
    # Container name and trailing argv are unchanged.
    assert container_idx + 1 < len(cmd), "argv must follow container name"
    assert cmd[container_idx + 1:] == ["claude", "--foo"], (
        f"argv must follow container, got {cmd[container_idx + 1:]!r}"
    )
    print("✓ DockerSession passes env dict via -e KEY=VALUE flags")


def test_docker_session_empty_env_skips_e_flags(monkeypatch) -> None:
    """When ``env={}`` or ``env=None`` is passed, the exec argv must
    NOT contain spurious empty ``-e`` flags like ``-e ""`` — those
    would be rejected by ``podman exec`` and surface as a confusing
    error to the user.
    """
    from src.profiles import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(
        name="NoEnv",
        slug="no-env",
        container_image="alpine:latest",
    )

    for empty_env in ({}, None):
        captured: dict = {}

        class _FakeProcess:
            def __init__(self, *args, **kwargs):
                captured["args"] = args

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(
            docker_session, "ensure_container",
            lambda profile: "no-env",
        )
        monkeypatch.setattr(
            docker_session, "_resolve_docker_binary", lambda: "docker"
        )
        monkeypatch.setattr(docker_session.subprocess, "Popen", _FakeProcess)
        monkeypatch.setattr(
            docker_session.DockerSession,
            "_reader_thread_target",
            lambda self: None,
        )

        session = docker_session.DockerSession(
            profile,
            argv=["claude"],
            env=empty_env,
        )
        session.run()

        cmd = captured["args"][0]
        assert "-e" not in cmd, (
            f"empty env must not produce -e flags, got cmd: {cmd!r} "
            f"(env was {empty_env!r})"
        )
        # The prefix is exactly ``docker exec -i -t`` and the container
        # name follows immediately.
        assert cmd[:4] == ["docker", "exec", "-i", "-t"]
        assert cmd[4] == "no-env"
    print("✓ DockerSession skips -e flags when env is empty or None")


def test_proxy_container_handles_docker_unavailable() -> None:
    """``start_proxy_container`` raises a clean ``FileNotFoundError``
    when the runtime binary is missing — the proxy lifecycle
    catches this and falls back to local ``QThread``. We test
    by pointing the resolver at a path that doesn't exist.
    """
    from src.profiles.global_settings import GlobalSettings
    from src.ui.claude import docker_proxy

    settings = GlobalSettings(container_runtime="nonexistent-binary-xyz")
    try:
        docker_proxy.start_proxy_container(settings, bind_port=3001)
    except FileNotFoundError as exc:
        assert "nonexistent-binary-xyz" in str(exc), exc
        print("✓ docker_proxy raises FileNotFoundError for missing runtime")
        return
    raise AssertionError("expected FileNotFoundError")


def test_proxy_container_is_running_returns_false_when_absent() -> None:
    """``is_proxy_container_running`` returns False when no
    container exists — the lifecycle uses this to decide
    whether to spawn a fresh container or reuse the existing one.
    """
    from src.profiles.global_settings import GlobalSettings
    from src.ui.claude import docker_proxy

    settings = GlobalSettings(container_runtime="nonexistent-binary-xyz")
    assert docker_proxy.is_proxy_container_running(settings, "sharkdrown-proxy") is False
    print("✓ is_proxy_container_running gracefully reports False for missing runtime")


def test_wait_for_proxy_ready_returns_false_for_unreachable() -> None:
    """Regression: ``wait_for_proxy_ready`` polls the proxy's
    ``/health`` endpoint and returns ``False`` within
    ``timeout_s`` when the host:port is unreachable. Without
    this wait the lifecycle would ``_on_proxy_started``
    immediately after ``docker run -d`` returns, before uvicorn
    has bound — and the GUI would show "cannot connect to
    proxy" while the container is still booting.
    """
    import socket
    import time

    from src.ui.claude.docker_proxy import wait_for_proxy_ready

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    start = time.monotonic()
    ready = wait_for_proxy_ready(
        "127.0.0.1", port, timeout_s=2.0, poll_interval_s=0.1
    )
    elapsed = time.monotonic() - start
    assert ready is False
    assert elapsed < 2.5, f"polled too long ({elapsed:.2f}s)"
    print("✓ wait_for_proxy_ready returns False for unreachable host/port")


def test_wait_for_proxy_ready_returns_true_for_running_server() -> None:
    """A simple HTTP server on a free port should be detected as
    ready by the poll loop within a few poll intervals.
    """
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from src.ui.claude.docker_proxy import wait_for_proxy_ready

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class _OK(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass  # quiet

    server = HTTPServer(("127.0.0.1", port), _OK)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ready = wait_for_proxy_ready(
            "127.0.0.1", port, timeout_s=5.0, poll_interval_s=0.1
        )
        assert ready is True
    finally:
        server.shutdown()
    print("✓ wait_for_proxy_ready detects a healthy /health endpoint")


def test_get_container_logs_returns_empty_when_runtime_missing() -> None:
    """``get_container_logs`` returns ``""`` when the runtime
    binary isn't on PATH — so the status-bar snippet stays
    clean (``"(no logs)"`` rather than a stack trace from a
    missing-binary ``FileNotFoundError``).
    """
    from src.profiles.global_settings import GlobalSettings
    from src.ui.claude import docker_proxy

    assert docker_proxy.get_container_logs(
        GlobalSettings(container_runtime="nonexistent-binary-xyz")
    ) == ""
    print("✓ get_container_logs returns empty string for missing runtime")


# ---- Bug: closing a Claude session left the container running ---------


def test_docker_session_stop_container_runs_runtime_stop(monkeypatch) -> None:
    """Bug regression: ``DockerSession.stop_container`` issues
    ``<runtime> stop <container>`` for the container captured at
    launch time.

    User-reported symptom: closing a Claude session that ran in
    a container left the container running indefinitely. Root
    cause was that no code path called ``<runtime> stop``; only
    the ``docker exec`` subprocess was killed.

    The fix introduces ``stop_container`` (and
    ``set_stop_container_on_shutdown``) so the per-profile
    container is torn down when the LAST session for the
    profile is closed.
    """
    from src.profiles.models import ClaudeProfile
    from src.ui.claude import docker_session

    # Stub the runtime resolver so the test doesn't depend on
    # what's installed in CI.
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        class _Result:
            returncode = 0
        return _Result()

    monkeypatch.setattr(docker_session.subprocess, "run", fake_run)

    profile = ClaudeProfile(name="p", slug="default")
    # ``DockerSession.__init__`` doesn't spawn anything, so we can
    # construct it without a real container.
    sess = docker_session.DockerSession(profile=profile, argv=["claude"])

    # Simulate the post-launch state: ``run`` recorded the
    # container name after ``ensure_container`` resolved it.
    sess._container_name = "sharkdrown-claude-default"

    sess.stop_container()

    assert captured, "stop_container should have invoked subprocess.run"
    cmd = captured[0]
    assert cmd[0] == "docker"
    assert cmd[1] == "stop"
    # Container name must be the LAST positional argument so
    # ``docker stop <name>`` parses correctly. (Earlier drafts of
    # the fix used ``--time 5 <name>`` interleaved — verify the
    # ordering rather than asserting exact args.)
    assert cmd[-1] == "sharkdrown-claude-default", (
        f"container name should be the last positional arg of "
        f"`docker stop ...`, got: {cmd!r}"
    )
    # Idempotency: a second call must NOT re-invoke the stop —
    # ``stop_container`` clears ``_container_name`` on success.
    captured.clear()
    sess.stop_container()
    assert captured == [], (
        "stop_container should be idempotent — second call with "
        "no container name should be a no-op"
    )
    print(
        "✓ DockerSession.stop_container runs `docker stop <name>` "
        "and is idempotent"
    )


def test_docker_session_set_stop_container_on_shutdown_default_true() -> None:
    """New ``DockerSession`` instances default
    ``_stop_container_on_shutdown=True`` so the container-stop
    runs on shutdown unless a sibling session opts out via
    ``set_stop_container_on_shutdown(False)``."""
    from src.profiles.models import ClaudeProfile
    from src.ui.claude import docker_session

    profile = ClaudeProfile(name="p", slug="default")
    sess = docker_session.DockerSession(profile=profile, argv=["claude"])
    assert sess._stop_container_on_shutdown is True, (
        "default must be True so a single-session profile "
        "automatically tears down its container"
    )
    # Setter flips it cleanly.
    sess.set_stop_container_on_shutdown(False)
    assert sess._stop_container_on_shutdown is False
    sess.set_stop_container_on_shutdown(True)
    assert sess._stop_container_on_shutdown is True
    print("✓ DockerSession.set_stop_container_on_shutdown toggles correctly")


def test_docker_session_stop_container_no_container_is_noop(monkeypatch) -> None:
    """``stop_container`` is a no-op when ``_container_name`` was
    never set (e.g. ``run`` failed before recording the name, or
    ``stop_container`` was already called once)."""
    from src.profiles.models import ClaudeProfile
    from src.ui.claude import docker_session

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return None

    monkeypatch.setattr(docker_session.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_session, "_resolve_docker_binary", lambda: "docker")

    profile = ClaudeProfile(name="p", slug="default")
    sess = docker_session.DockerSession(profile=profile, argv=["claude"])
    # No ``_container_name`` set — session never reached ``run``.
    sess.stop_container()
    assert captured == [], (
        "stop_container with no container name should be a no-op; "
        f"got subprocess.run calls: {captured!r}"
    )
    print(
        "✓ DockerSession.stop_container is a no-op without a "
        "container name"
    )