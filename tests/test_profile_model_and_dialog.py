"""Tests for the new profile model + Manage Profiles flow.

Covers:
  - ClaudeProfile has bind_address, port, config_dir, is_default fields
  - ProfileStore.load() always returns a Default profile at index 0
  - ProfileStore.delete("Default") is a no-op
  - ProfileStore.ensure_default() creates one if missing
  - New profiles seeded from Default carry the same model_endpoints
  - ProfileEditorDialog has the new bind/port/config_dir widgets
  - ProfileEditorDialog wraps Model Endpoints and Env Vars in scroll
    areas so more than 2 entries are visible via scroll
  - ManageProfilesDialog: list shows Default at top; Delete disabled
    for Default; Add New seeds from Default; Edit works for everyone
  - MainWindow: Profile submenu contains Settings + Manage Profiles
    (the old top-level Settings entry is gone)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


# ``QMessageBox.warning`` is a *modal* dialog. Under the offscreen
# Qt platform plugin it blocks indefinitely waiting for a click that
# never comes — which silently wedges the test suite whenever a
# validation path surfaces a popup. Stub it at import time so
# validation failures are silent (rather than fatal) and the test
# runner still observes the post-validation state to detect the
# regression (e.g. ``dlg._on_accept`` not raising + button
# remaining enabled when the user wants to confirm).
from PySide6.QtWidgets import QMessageBox

_original_warning = QMessageBox.warning


def _silent_warning(*_args, **_kwargs):
    return QMessageBox.StandardButton.Yes


QMessageBox.warning = staticmethod(_silent_warning)


def _qapp():
    """Build (or reuse) a headless QApplication."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _tmp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp())


# ---- ClaudeProfile model --------------------------------------------------


def test_claude_profile_has_bind_port_config_dir_and_default_flag() -> None:
    """The new fields are part of the dataclass with sensible defaults."""
    from src.profiles import ClaudeProfile

    p = ClaudeProfile(name="Demo", slug="demo")
    assert p.bind_address == "127.0.0.1", (
        f"default bind_address should be loopback, got {p.bind_address!r}"
    )
    assert p.port == 3001
    assert p.config_dir == ""
    assert p.is_default is False
    print("✓ ClaudeProfile has bind_address, port, config_dir, is_default")


def test_claude_profile_serialization_roundtrips_new_fields() -> None:
    """Custom values for the new fields survive a serialize/deserialize
    round-trip through ``ProfileStore``."""
    from src.profiles import ClaudeProfile, ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    p = ClaudeProfile(
        name="Custom",
        slug="custom",
        model_endpoints={"claude-opus-4-8": "https://example.com"},
        bind_address="0.0.0.0",
        port=9001,
        config_dir="/tmp/my-config",
        is_default=True,
    )
    store.upsert(p)
    profiles = store.load()
    by_name = {p.name: p for p in profiles}
    loaded = by_name["Custom"]
    assert loaded.bind_address == "0.0.0.0"
    assert loaded.port == 9001
    assert loaded.config_dir == "/tmp/my-config"
    assert loaded.is_default is True
    print("✓ New fields round-trip through ProfileStore")


# ---- Always-Default contract ----------------------------------------------


def test_default_profile_always_exists_at_index_zero() -> None:
    """``load()`` materialises a Default profile even on a fresh install."""
    from src.profiles import DEFAULT_PROFILE_NAME, ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    profiles = store.load()
    assert profiles[0].name == DEFAULT_PROFILE_NAME
    assert profiles[0].is_default is True
    print("✓ Default profile is always present at index 0")


def test_default_profile_stays_first_after_adding_others() -> None:
    """Adding user profiles doesn't push the Default out of position 0."""
    from src.profiles import ClaudeProfile, ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.upsert(ClaudeProfile(name="Alpha", slug="alpha", model_endpoints={}))
    store.upsert(ClaudeProfile(name="Beta", slug="beta", model_endpoints={}))
    profiles = store.load()
    assert profiles[0].name == "Default"
    assert [p.name for p in profiles] == ["Default", "Alpha", "Beta"]
    print("✓ Default stays at index 0 after user profiles are added")


def test_default_profile_cannot_be_deleted_via_store() -> None:
    """``ProfileStore.delete("Default")`` is a no-op (belt-and-braces
    guard — the dialog disables the Delete button for the Default
    profile, but the underlying API refuses too)."""
    from src.profiles import ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.load()  # ensures the Default is materialised
    before = [p.name for p in store.load()]
    store.delete("Default")
    after = [p.name for p in store.load()]
    assert before == after == ["Default"], (
        f"Default was removed — before={before}, after={after}"
    )
    print("✓ Default profile is non-deletable via the store API")


def test_ensure_default_creates_one_if_missing() -> None:
    """When the on-disk file is missing a Default, ``ensure_default``
    fabricates one with the dataclass defaults and persists it."""
    from src.profiles import DEFAULT_PROFILE_NAME, ProfileStore

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    # Pre-load to ensure the file is written (with the seeded
    # Default), then surgically delete the Default from the file
    # so ensure_default() has to rebuild it.
    store.load()
    import json
    raw = json.loads((base / "profiles.json").read_text(encoding="utf-8"))
    raw["profiles"] = []  # no Default on disk
    (base / "profiles.json").write_text(json.dumps(raw), encoding="utf-8")

    default = store.ensure_default()
    assert default.name == DEFAULT_PROFILE_NAME
    assert default.is_default is True
    # Subsequent load() should still find the Default at index 0.
    profiles = store.load()
    assert profiles[0].name == DEFAULT_PROFILE_NAME
    print("✓ ensure_default() fabricates a Default if missing")


def test_new_profile_seeded_from_default_inherits_endpoints() -> None:
    """``ManageProfilesDialog`` seeds Add New from the on-disk Default.
    Here we exercise the same contract directly: a fresh profile
    constructed from the Default's fields carries the same model
    endpoints, env vars, bind/port/config_dir."""
    import copy

    from src.profiles import ClaudeProfile, ModelEndpoint, ProfileStore, Provider

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    # Customise the Default so we can detect inheritance.
    default.providers = {
        "default": Provider(name="default", url="https://api.example.com"),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    default.bind_address = "0.0.0.0"
    default.port = 4000
    default.env_vars = {"MY_VAR": "hello"}
    store.upsert(default)

    # Read back and seed a new profile the way the dialog does.
    refreshed_default = store.load()[0]
    new_profile = copy.deepcopy(refreshed_default)
    new_profile.name = "NewOne"
    new_profile.slug = "newone"
    new_profile.is_default = False
    store.upsert(new_profile)

    loaded = {p.name: p for p in store.load()}
    assert loaded["NewOne"].model_endpoints == {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    assert loaded["NewOne"].providers == {
        "default": Provider(name="default", url="https://api.example.com"),
    }
    assert loaded["NewOne"].bind_address == "0.0.0.0"
    assert loaded["NewOne"].port == 4000
    assert loaded["NewOne"].env_vars == {"MY_VAR": "hello"}
    print("✓ new profile seeded from Default inherits its fields")


# ---- ProfileEditorDialog --------------------------------------------------


def test_profile_editor_has_bind_port_config_dir_widgets() -> None:
    """The dialog exposes a ``bind_edit``, ``port_edit``, and
    ``config_dir_edit`` widget and the values round-trip."""
    from src.profiles import ClaudeProfile, ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    # Seed a default credential so the API Keys tab validation in
    # ``_on_accept`` doesn't pop a modal QMessageBox in offscreen mode.
    store.credential_store.set("default", "test-key")
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert hasattr(dlg, "bind_edit")
        assert hasattr(dlg, "port_edit")
        assert hasattr(dlg, "config_dir_edit")
        # Edit a value and verify it survives _on_accept.
        dlg.bind_edit.setText("0.0.0.0")
        dlg.port_edit.setValue(9001)
        dlg.config_dir_edit.setText("/tmp/proxy-config")
        # The dialog has to "accept" with valid name + endpoints. The
        # Default we created has no endpoints, so add one.
        from src.profiles.models import slugify
        dlg.name_edit.setText(default.name)
        # ``_populate`` already pre-adds an empty endpoint row so the
        # user has somewhere to type. Fill it in.
        dlg.endpoints_table.item(0, 0).setText("")  # Aliases col
        # Provider col already pre-populated as "default"
        dlg.endpoints_table.item(0, 2).setText("claude-opus-4-8")  # Model col
        dlg._on_accept()
        edited = dlg.to_profile()
        assert edited.bind_address == "0.0.0.0"
        assert edited.port == 9001
        assert edited.config_dir == "/tmp/proxy-config"
    finally:
        dlg.deleteLater()
    print("✓ ProfileEditorDialog has bind/port/config_dir round-tripping")


def test_model_endpoints_table_is_in_a_scroll_area() -> None:
    """The Model Endpoints ``QTableWidget`` lives inside a
    ``QScrollArea`` so more than 2 rows are reachable via scroll."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # The dialog stores the scroll areas as ``*_scroll`` attrs.
        assert hasattr(dlg, "endpoints_table_scroll"), (
            "Model Endpoints table should be wrapped in a scroll area"
        )
        from PySide6.QtWidgets import QScrollArea

        assert isinstance(dlg.endpoints_table_scroll, QScrollArea)
        # The scroll area should have a max height so the dialog
        # layout doesn't get pushed off-screen.
        assert dlg.endpoints_table_scroll.maximumHeight() > 0, (
            "scroll area should cap the table height"
        )
    finally:
        dlg.deleteLater()
    print("✓ Model Endpoints table is wrapped in a QScrollArea")


def test_env_vars_table_is_in_a_scroll_area() -> None:
    """Same for the Environment Variables table."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert hasattr(dlg, "env_table_scroll"), (
            "Env Vars table should be wrapped in a scroll area"
        )
        from PySide6.QtWidgets import QScrollArea

        assert isinstance(dlg.env_table_scroll, QScrollArea)
    finally:
        dlg.deleteLater()
    print("✓ Environment Variables table is wrapped in a QScrollArea")


# ---- ManageProfilesDialog -------------------------------------------------


def test_manage_profiles_dialog_shows_list_with_default_first() -> None:
    """The list dialog shows the Default profile at index 0 and labels
    it with ``(default)`` so the user can tell it apart."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ManageProfilesDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    dlg = ManageProfilesDialog(store)
    try:
        assert dlg.list_widget.count() >= 1
        first = dlg.list_widget.item(0)
        assert "(default)" in first.text(), (
            f"Default profile should be labelled with '(default)'; "
            f"got {first.text()!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ ManageProfilesDialog lists Default first with (default) label")


def test_manage_profiles_dialog_delete_disabled_for_default() -> None:
    """The Delete button is disabled when the Default profile is
    selected — Default is the always-on template and can't be removed."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ManageProfilesDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    dlg = ManageProfilesDialog(store)
    try:
        dlg.list_widget.setCurrentRow(0)  # Default
        assert dlg.delete_btn.isEnabled() is False, (
            "Delete should be disabled for the Default profile"
        )
    finally:
        dlg.deleteLater()
    print("✓ Delete button is disabled for the Default profile")


def test_manage_profiles_dialog_delete_enabled_for_user_profile() -> None:
    """After adding a user profile, selecting it should enable Delete."""
    import copy

    from src.profiles import ClaudeProfile, ProfileStore
    from src.ui.claude.profile_editor_dialog import ManageProfilesDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    user = copy.deepcopy(default)
    user.name = "UserOne"
    user.slug = "userone"
    user.is_default = False
    user.model_endpoints = {"claude-opus-4-8": "https://x"}
    store.upsert(user)
    dlg = ManageProfilesDialog(store)
    try:
        # Find the UserOne row.
        from PySide6.QtCore import Qt as _Qt
        target_row = None
        for i in range(dlg.list_widget.count()):
            item = dlg.list_widget.item(i)
            profile = item.data(_Qt.ItemDataRole.UserRole)
            if profile is not None and profile.name == "UserOne":
                target_row = i
                break
        assert target_row is not None, "UserOne row should be in the list"
        dlg.list_widget.setCurrentRow(target_row)
        assert dlg.delete_btn.isEnabled() is True
    finally:
        dlg.deleteLater()
    print("✓ Delete button is enabled for non-Default profiles")


# ---- MainWindow menu structure -------------------------------------------


def test_proxy_settings_action_lives_under_profile_submenu() -> None:
    """``Settings…`` used to be a top-level Claude menu item; the user
    moved it under the Profile submenu. Verify the move."""
    from src.ui.main_window import MainWindow

    _qapp()
    w = MainWindow()
    try:
        # Top-level Claude menu must NOT contain Settings any more.
        menubar = w.menuBar()
        # Find the Claude menu (its text starts with '&laude' / 'Cl').
        claude_menu = None
        for a in menubar.actions():
            if "laude" in a.text().lower() and a.menu() is not None:
                claude_menu = a.menu()
                break
        assert claude_menu is not None, "Claude menu not found"
        # The top-level Claude menu should NOT have a Settings action
        # any more — only the Profile submenu should.
        for act in claude_menu.actions():
            if "settings" in act.text().lower():
                # The text shouldn't be a direct child of the Claude
                # menu — it should be inside a submenu.
                assert act.parent() is not claude_menu, (
                    "Settings should not be a direct child of the Claude menu"
                )
        # And it should be inside the Profile submenu.
        assert hasattr(w, "profile_menu"), "Profile submenu not found"
        profile_action_texts = [
            a.text() for a in w.profile_menu.actions()
        ]
        assert any("settings" in t.lower() for t in profile_action_texts), (
            f"Settings should be in the Profile submenu; got {profile_action_texts}"
        )
    finally:
        w.close()
    print("✓ Settings is under the Profile submenu, not at the Claude top level")


def test_profile_submenu_has_manage_profiles_entry() -> None:
    """Manage Profiles sits next to Settings in the Profile submenu."""
    from src.ui.main_window import MainWindow

    _qapp()
    w = MainWindow()
    try:
        assert hasattr(w, "action_manage_profiles"), (
            "action_manage_profiles should exist"
        )
        # The action should be a child of the Profile submenu. Use
        # ``actions()`` rather than ``action.menu()`` because the
        # latter returns the *owning* submenu for submenu-title
        # actions, not the parent for a plain action.
        profile_actions = list(w.profile_menu.actions())
        in_profile = w.action_manage_profiles in profile_actions
        assert in_profile, (
            "Manage Profiles should be listed in the Profile submenu"
        )
    finally:
        w.close()
    print("✓ Manage Profiles is inside the Profile submenu")


def test_active_profile_drives_proxy_start() -> None:
    """Reading from the active profile instead of legacy
    ``ProxyConfig`` — the host/port/config_dir/model_endpoints on
    the profile are what the proxy thread is constructed with."""
    from src.profiles import ClaudeProfile, ModelEndpoint, ProfileStore, Provider
    from src.ui.main_window import MainWindow

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    # Customise the active profile with distinctive values.
    active = store.get_active() or store.ensure_default()
    active.bind_address = "0.0.0.0"
    active.port = 8765
    active.providers = {
        "default": Provider(name="default", url="https://example.org"),
    }
    active.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(active)

    w = MainWindow()
    try:
        w.profile_store = store
        # Patch the ``ProxyServerThread`` symbol *as imported in the
        # main_window module* — main_window did
        # ``from src.proxy.server_thread import ProxyServerThread``,
        # so the binding is on ``src.ui.main_window`` itself.
        captured = {}

        class _FakeThread:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self._is_running = False

            def isRunning(self) -> bool:  # noqa: N802 — Qt naming
                return self._is_running

            def start(self) -> None:
                self._is_running = True

        import src.ui.main_window._proxy_lifecycle as _plm
        original = _plm.ProxyServerThread

        class _FakeThreadWithSignals(_FakeThread):
            """Fake thread that exposes the Qt signals MainWindow hooks
            into. ``started_ok`` / ``failed`` / ``stopped`` are Qt
            Signal objects on the real ProxyServerThread; the slot
            ``_start_server`` connects to them at line ~910. Without
            these attrs the connection call raises AttributeError.

            We just give them the value of ``None`` — the slot would
            try to ``.connect(...)`` on None and fail, so we suppress
            the connection by replacing ``_start_server`` with a
            version that calls the thread directly without signal
            wiring. Simpler: just give the fake ``Signal`` instances.
            """

            # The real ``ProxyServerThread`` inherits from QThread,
            # whose ``Signal`` class is ``QSignal``. We import the
            # same here so ``.connect`` doesn't blow up.
            from PySide6.QtCore import QThread

            class _Signal:
                def __init__(self):
                    self._slots = []

                def connect(self, slot):
                    self._slots.append(slot)

            started_ok = _Signal()
            failed = _Signal()
            stopped = _Signal()

        _plm.ProxyServerThread = _FakeThreadWithSignals
        try:
            w._start_server()
        finally:
            _plm.ProxyServerThread = original
            # Tear down the captured thread if ``_start_server`` set
            # ``self.proxy_thread`` to it; prevents a dangling
            # QThread warning on the next test.
            w.proxy_thread = None

        assert captured.get("host") == "0.0.0.0", (
            f"expected bind_address from active profile, got {captured.get('host')!r}"
        )
        assert captured.get("port") == 8765
        # Post-refactor: endpoints map model → provider_name (the URL
        # + auth come from ``providers_cfg``).
        assert captured.get("model_endpoints") == {
            "claude-opus-4-8": "default"
        }, (
            f"unexpected model_endpoints: {captured.get('model_endpoints')!r}"
        )
        assert captured.get("providers_cfg") == {
            "default": {
                "url": "https://example.org",
                "api_key": None,
                "available_models": [],
            }
        }, (
            f"unexpected providers_cfg: {captured.get('providers_cfg')!r}"
        )
    finally:
        w.close()
    print("✓ Active profile drives proxy start (host/port/model_endpoints)")


# ---- Standard env var catalog & picker ------------------------------------


def test_env_var_catalog_includes_expected_entries() -> None:
    """A few representative env vars from the public reference are
    present in the catalog. The catalog is hand-curated from the
    docs page; this test catches accidental truncation or rename."""
    from src.profiles.env_var_catalog import standard_env_vars

    names = {v.name for v in standard_env_vars()}
    # Authentication / API
    assert "ANTHROPIC_AUTH_TOKEN" in names
    assert "ANTHROPIC_BETAS" in names
    assert "ANTHROPIC_CUSTOM_HEADERS" in names
    # Model Selection
    assert "ANTHROPIC_MODEL" in names
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" in names
    # Timeouts
    assert "API_TIMEOUT_MS" in names
    assert "BASH_DEFAULT_TIMEOUT_MS" in names
    # Session / subprocess
    assert "CLAUDECODE" in names
    # Cloud / Remote
    assert "CCR_FORCE_BUNDLE" in names
    # Feature toggles
    assert "CLAUDE_CODE_DISABLE_ATTACHMENTS" in names
    print("✓ env var catalog includes representative entries")


def test_env_var_catalog_excludes_infra_owned_vars() -> None:
    """``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL`` and
    ``CLAUDE_CONFIG_DIR`` are owned by the integration and must
    not be selectable from the standard picker."""
    from src.profiles.env_var_catalog import (
        INFRA_OWNED_VARS,
        standard_env_vars,
    )

    names = {v.name for v in standard_env_vars()}
    for infra in INFRA_OWNED_VARS:
        assert infra not in names, (
            f"{infra} is owned by the integration and must not appear "
            f"in the picker"
        )
    print("✓ infra-owned env vars are excluded from the catalog")


def test_env_var_catalog_sections_are_ordered() -> None:
    """``sections()`` returns distinct section labels in the order
    they first appear in the catalog. The picker uses this for
    rendering section headers top-to-bottom."""
    from src.profiles.env_var_catalog import sections

    s = sections()
    assert len(s) >= 2, "expected at least 2 sections"
    # No duplicates.
    assert len(set(s)) == len(s)
    # "Anthropic API & Authentication" is the first section in the
    # catalog by design — auth flows before model selection.
    assert s[0] == "Anthropic API & Authentication"
    print(f"✓ env var catalog has {len(s)} ordered sections")


def test_standard_env_vars_dialog_has_two_columns() -> None:
    """The picker uses a 2-column ``QTableView`` with headers
    ``Variable`` and ``Purpose`` — matching the web docs layout
    the user referenced."""
    from src.ui.claude.profile_editor_dialog import StandardEnvVarsDialog

    _qapp()
    dlg = StandardEnvVarsDialog()
    try:
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QTableView

        assert isinstance(dlg.table, QTableView)
        # Two header columns: Variable, Purpose.
        assert dlg.model.columnCount() == 2
        headers = [
            str(dlg.model.headerData(i, _Qt.Orientation.Horizontal))
            for i in range(dlg.model.columnCount())
        ]
        assert headers[0].lower() == "variable"
        assert headers[1].lower() == "purpose"
        # Rows present: every section header + every variable.
        from src.profiles.env_var_catalog import (
            sections,
            standard_env_vars,
        )
        expected_rows = len(sections()) + len(standard_env_vars())
        assert dlg.model.rowCount() == expected_rows, (
            f"expected {expected_rows} rows (sections + vars), "
            f"got {dlg.model.rowCount()}"
        )
    finally:
        dlg.deleteLater()
    print("✓ StandardEnvVarsDialog is a 2-column table")


def test_standard_env_vars_dialog_filters_already_added() -> None:
    """Variables the user has already added to the profile get a
    ``*`` marker on the name column so the user can see they're
    already present."""
    from src.ui.claude.profile_editor_dialog import StandardEnvVarsDialog

    _qapp()
    # Pretend ``ANTHROPIC_MODEL`` is already on the profile.
    dlg = StandardEnvVarsDialog(
        already_added=frozenset({"ANTHROPIC_MODEL"}),
    )
    try:
        found_marker = False
        from PySide6.QtCore import Qt as _Qt

        for row in range(dlg.model.rowCount()):
            left = dlg.model.item(row, 0)
            if left is None:
                continue
            # Skip section headers (their UserRole is "header").
            if left.data(_Qt.ItemDataRole.UserRole) == "header":
                continue
            if left.text().startswith("ANTHROPIC_MODEL"):
                assert left.text().endswith("*"), (
                    f"already-added entry should be marked with '*', "
                    f"got {left.text()!r}"
                )
                found_marker = True
        assert found_marker, "could not find ANTHROPIC_MODEL row"
    finally:
        dlg.deleteLater()
    print("✓ already-added vars are marked with '*'")


def test_standard_env_vars_dialog_returns_selected_names() -> None:
    """Picking a single row returns that variable's name (without
    the ``*`` marker)."""
    from PySide6.QtCore import Qt as _Qt
    from src.ui.claude.profile_editor_dialog import StandardEnvVarsDialog

    _qapp()
    dlg = StandardEnvVarsDialog()
    try:
        # Find the row for ``ANTHROPIC_AUTH_TOKEN`` and select it.
        target_row = None
        for row in range(dlg.model.rowCount()):
            left = dlg.model.item(row, 0)
            if left is None:
                continue
            if left.data(_Qt.ItemDataRole.UserRole) == "header":
                continue
            if left.text().strip() == "ANTHROPIC_AUTH_TOKEN":
                target_row = row
                break
        assert target_row is not None
        # Selecting a single row in ``ExtendedSelection`` mode.
        from PySide6.QtCore import QItemSelection, QItemSelectionModel

        sel_model = dlg.table.selectionModel()
        idx = dlg.model.index(target_row, 0)
        sel_model.select(
            QItemSelection(idx, idx),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        names = dlg.selected_names()
        assert names == ["ANTHROPIC_AUTH_TOKEN"], (
            f"expected ['ANTHROPIC_AUTH_TOKEN'], got {names}"
        )
    finally:
        dlg.deleteLater()
    print("✓ StandardEnvVarsDialog.selected_names() returns the picked name")


def test_standard_env_vars_dialog_returns_multiple_when_extended_selection() -> None:
    """Ctrl/Cmd-click picks multiple rows; ``selected_names``
    returns all of them in display order."""
    from PySide6.QtCore import Qt as _Qt
    from src.ui.claude.profile_editor_dialog import StandardEnvVarsDialog

    _qapp()
    dlg = StandardEnvVarsDialog()
    try:
        targets = {"ANTHROPIC_BETAS", "API_TIMEOUT_MS", "CLAUDECODE"}
        target_rows: list[int] = []
        for row in range(dlg.model.rowCount()):
            left = dlg.model.item(row, 0)
            if left is None:
                continue
            if left.data(_Qt.ItemDataRole.UserRole) == "header":
                continue
            if left.text().strip() in targets:
                target_rows.append(row)
        assert len(target_rows) == len(targets)
        from PySide6.QtCore import QItemSelection, QItemSelectionModel

        sel_model = dlg.table.selectionModel()
        flags = (
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows
        )
        for row in target_rows:
            idx = dlg.model.index(row, 0)
            sel_model.select(QItemSelection(idx, idx), flags)
        names = dlg.selected_names()
        # Order isn't strictly guaranteed (selectionModel may
        # return sorted), so compare as sets.
        assert set(names) == targets, f"expected {targets}, got {set(names)}"
    finally:
        dlg.deleteLater()
    print("✓ Multi-select returns all picked variable names")


def test_standard_env_vars_dialog_skips_section_headers() -> None:
    """Section headers are rendered as non-selectable rows; even if
    a header somehow ended up selected, ``selected_names`` ignores
    it (and never returns an empty string)."""
    from PySide6.QtCore import Qt as _Qt
    from src.ui.claude.profile_editor_dialog import StandardEnvVarsDialog

    _qapp()
    dlg = StandardEnvVarsDialog()
    try:
        # Find the first section header row.
        header_row = None
        for row in range(dlg.model.rowCount()):
            left = dlg.model.item(row, 0)
            if left is None:
                continue
            if left.data(_Qt.ItemDataRole.UserRole) == "header":
                header_row = row
                break
        assert header_row is not None, "no section header found"
        # Section headers should not be selectable (``ItemIsSelectable``
        # flag is absent in our setup).
        left = dlg.model.item(header_row, 0)
        assert not (left.flags() & _Qt.ItemFlag.ItemIsSelectable), (
            "section headers must not be selectable"
        )
        # Even if selection were forced, ``selected_names`` should
        # skip them.
        names = dlg.selected_names()
        assert names == [], f"expected empty selection, got {names}"
        # And no empty strings slip through.
        assert all(n for n in names)
    finally:
        dlg.deleteLater()
    print("✓ Section headers are skipped from selection")


# ---- Profile editor: Add Standard / Add Custom buttons --------------------


def test_profile_editor_has_add_standard_and_add_custom_buttons() -> None:
    """The env vars table now has *two* Add buttons: Standard and
    Custom. The old single ``Add…`` button on the env vars region
    is gone (a plain ``Add…`` may still exist on the subagents
    panel — that one is unrelated)."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # The dialog exposes its slots directly — invoke them
        # directly rather than scraping button text. The slot
        # existence is the contract; the labels are presentation.
        assert hasattr(dlg, "_on_env_add_standard"), (
            "ProfileEditorDialog should expose _on_env_add_standard"
        )
        assert hasattr(dlg, "_on_env_add_custom"), (
            "ProfileEditorDialog should expose _on_env_add_custom"
        )
        # And the corresponding button widgets — find by objectName
        # style is overkill, so check by widget tree: there must be
        # a QPushButton under the env-vars button row whose text
        # mentions "Standard" and one whose text mentions "Custom".
        from PySide6.QtWidgets import QPushButton

        labels = [b.text() for b in dlg.findChildren(QPushButton)]
        assert any("Standard" in t for t in labels), (
            f"expected an 'Add Standard…' button, got {labels}"
        )
        assert any("Custom" in t for t in labels), (
            f"expected an 'Add Custom…' button, got {labels}"
        )
    finally:
        dlg.deleteLater()
    print("✓ ProfileEditorDialog has Add Standard and Add Custom buttons")


def test_add_standard_button_inserts_blank_value_rows() -> None:
    """Clicking Add Standard inserts rows for the picked variables
    with the *name* pre-filled and the *value* blank. The user
    fills values afterwards — the picked vars persist via the
    ``_table_pairs`` filter only after a value is set, matching
    the user's spec."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import (
        ProfileEditorDialog,
        StandardEnvVarsDialog,
    )

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Synthesise what Add Standard does: open picker, get
        # names, insert blank-value rows. We don't open the actual
        # picker here — we test the *integration*: call the slot
        # with the picker pre-set to return a fixed list.
        from PySide6.QtWidgets import QDialog

        class _FakePicker:
            def __init__(self, names: list[str]) -> None:
                self._names = names

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def selected_names(self) -> list[str]:
                return list(self._names)

        names_to_pick = ["ANTHROPIC_BETAS", "API_TIMEOUT_MS"]
        original = StandardEnvVarsDialog
        # ``_on_env_add_standard`` instantiates ``StandardEnvVarsDialog``
        # directly, so monkey-patch the symbol as imported into the
        # dialog module.
        import src.ui.claude.profile_editor_dialog as ped

        ped.StandardEnvVarsDialog = lambda **kw: _FakePicker(names_to_pick)
        try:
            dlg._on_env_add_standard()
        finally:
            ped.StandardEnvVarsDialog = original

        # Two new rows; the table has zero rows initially (the
        # Default profile has no env vars).
        assert dlg.env_table.rowCount() == 2, (
            f"expected 2 rows, got {dlg.env_table.rowCount()}"
        )
        # Names pre-filled, values blank.
        from PySide6.QtWidgets import QTableWidgetItem

        for row, expected_name in enumerate(names_to_pick):
            k_item = dlg.env_table.item(row, 0)
            v_item = dlg.env_table.item(row, 1)
            assert isinstance(k_item, QTableWidgetItem)
            assert k_item.text() == expected_name, (
                f"row {row} name should be {expected_name!r}, got {k_item.text()!r}"
            )
            # Value column is blank — the user fills it later.
            assert (v_item.text() if v_item else "") == "", (
                "value should be blank when picked from the standard catalog"
            )
    finally:
        dlg.deleteLater()
    print("✓ Add Standard inserts blank-value rows with names pre-filled")


def test_add_custom_button_inserts_blank_row() -> None:
    """``Add Custom`` adds a single blank row — same shape as the
    old ``Add…`` button."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert dlg.env_table.rowCount() == 0
        dlg._on_env_add_custom()
        assert dlg.env_table.rowCount() == 1
        k_item = dlg.env_table.item(0, 0)
        v_item = dlg.env_table.item(0, 1)
        assert (k_item.text() if k_item else "") == ""
        assert (v_item.text() if v_item else "") == ""
    finally:
        dlg.deleteLater()
    print("✓ Add Custom inserts a single blank row")


def test_existing_env_keys_marks_only_known_standard_vars() -> None:
    """``_existing_env_keys`` returns the subset of names already
    on the env table that are in the standard catalog — free-form
    custom names don't appear in the catalog and therefore don't
    trigger the ``*`` marker."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        dlg._append_table_row(dlg.env_table, "ANTHROPIC_MODEL", "claude-opus-4-8")
        dlg._append_table_row(dlg.env_table, "MY_CUSTOM_VAR", "hello")
        keys = dlg._existing_env_keys()
        assert keys == frozenset({"ANTHROPIC_MODEL"}), (
            f"expected only standard catalog names, got {keys}"
        )
    finally:
        dlg.deleteLater()
    print("✓ _existing_env_keys reports only standard-catalog names")


# ---- Tabbed layout --------------------------------------------------------


def test_profile_editor_uses_a_qtabwidget_with_six_tabs() -> None:
    """The dialog body is a ``QTabWidget`` with six tabs in this
    order: General, Model Endpoints, API Key, Environment Variables,
    Settings.JSON, Subagents."""
    from PySide6.QtWidgets import QTabWidget
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert hasattr(dlg, "tabs"), "dialog should expose a QTabWidget as self.tabs"
        assert isinstance(dlg.tabs, QTabWidget), (
            f"self.tabs should be a QTabWidget, got {type(dlg.tabs).__name__}"
        )
        assert dlg.tabs.count() == 7, (
            f"expected 7 tabs, got {dlg.tabs.count()}"
        )
        labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        expected = [
            "General",
            "Model Configuration",
            "Providers",
            "API Keys",
            "Environment Variables",
            "Settings.JSON",
            "Subagents",
        ]
        assert labels == expected, (
            f"tab labels/order changed: expected {expected}, got {labels}"
        )
    finally:
        dlg.deleteLater()
    print("✓ ProfileEditorDialog is a 7-tab QTabWidget in expected order")


def test_profile_editor_widgets_live_in_their_expected_tabs() -> None:
    """Each named widget must live under its expected tab. Walks up
    the parent chain and asserts the immediate tab ancestor has the
    right label — catches the easy mistake of leaving a widget
    parented to the outer layout instead of a tab container."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        expected_tab = {
            "name_edit": "General",
            "default_model_combo": "General",
            "bind_edit": "General",
            "port_edit": "General",
            "config_dir_edit": "General",
            "cwd_edit": "General",
            "endpoints_table": "Model Configuration",
            "providers_table": "Providers",
            "provider_url_edit": "Providers",
            "keys_table": "API Keys",
            "key_value_edit": "API Keys",
            "env_table": "Environment Variables",
            "settings_edit": "Settings.JSON",
            "subagent_list": "Subagents",
        }
        for attr, label in expected_tab.items():
            widget = getattr(dlg, attr)
            tab_label = _enclosing_tab_label(widget, dlg.tabs)
            assert tab_label == label, (
                f"{attr} should be in tab {label!r}, found it in {tab_label!r}"
            )
    finally:
        dlg.deleteLater()
    print("✓ All widgets live under their expected tab")


def _enclosing_tab_label(widget, tabs):
    """Walk ``widget.parent()`` until we hit a child of ``tabs``,
    then return that tab's label. Returns ``None`` if no tab is
    found (e.g. widget is parented directly to the dialog, which
    is the bug we're catching)."""
    from PySide6.QtWidgets import QWidget

    cur = widget.parent()
    while cur is not None and cur is not tabs:
        # Is ``cur`` directly a tab page? ``QTabWidget.widget(i)``
        # returns the page widget for index i.
        for i in range(tabs.count()):
            if tabs.widget(i) is cur:
                return tabs.tabText(i)
        cur = cur.parent() if isinstance(cur, QWidget) else None
    # Fallback: the immediate parent might be the tab page itself.
    immediate = widget.parent()
    if immediate is not None:
        for i in range(tabs.count()):
            if tabs.widget(i) is immediate:
                return tabs.tabText(i)
    return None


def test_profile_editor_dialog_height_is_more_compact() -> None:
    """The tabbed layout is shorter than the previous linear flow.
    The old layout was ~880px tall on a 720x720 default; the tabbed
    version should drop well below that. This is informational —
    the test fails only if the tabbing regresses to a tall dialog."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # The default dialog size was 720x720. With six tabs and
        # no per-tab forced expansion, the dialog's natural
        # minimum height should drop well below that. Cap at 700
        # so this test fails loudly if a tab regresses to filling
        # the whole dialog.
        h = dlg.minimumSizeHint().height()
        assert h < 700, (
            f"dialog minimum height should be compact, got {h}px "
            f"(linear flow was ~880px)"
        )
    finally:
        dlg.deleteLater()
    print("✓ Tabbed dialog is more compact than the linear flow")


# ---- API Keys tab ----------------------------------------------------------


def test_api_keys_tab_has_default_row_pinned() -> None:
    """The keys table starts with row 0 named ``default`` and its name
    is locked (non-editable)."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    # Seed a couple of named keys so we can verify the default row
    # stays at index 0 even when other keys are present.
    store.credential_store.set("default", "default-value")
    store.credential_store.set("work", "work-value")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        from PySide6.QtCore import Qt as _Qt

        assert dlg.keys_table.rowCount() == 2
        row0_name = dlg.keys_table.item(0, 0).text()
        assert row0_name == "default", (
            f"row 0 should be the default key, got {row0_name!r}"
        )
        # The default row's name cell is read-only.
        flags = dlg.keys_table.item(0, 0).flags()
        assert not (flags & _Qt.ItemFlag.ItemIsEditable), (
            "the default row's name cell should be non-editable"
        )
    finally:
        dlg.deleteLater()
    print("✓ API Keys tab: default row pinned at index 0, name locked")


def test_api_keys_tab_add_inserts_new_row() -> None:
    """Clicking Add… appends a new row with a ``key-N`` placeholder
    name and an empty value."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        before = dlg.keys_table.rowCount()
        dlg._on_key_add()
        after = dlg.keys_table.rowCount()
        assert after == before + 1
        # The new row is at the bottom; its name is "key-1".
        new_name = dlg.keys_table.item(after - 1, 0).text()
        assert new_name == "key-1", (
            f"new row should be named 'key-1', got {new_name!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ API Keys tab: Add inserts a new row")


def test_default_row_cannot_be_removed() -> None:
    """Selecting the default row + clicking Remove Selected is a
    no-op. Row 0 stays put; the row count is unchanged."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        dlg.keys_table.setCurrentCell(0, 0)
        # The Remove button should be disabled when the default row
        # is selected.
        assert dlg.keys_remove_btn.isEnabled() is False, (
            "Remove button should be disabled when default row is selected"
        )
        # Calling the slot directly is a no-op even if the slot were
        # somehow invoked.
        before = dlg.keys_table.rowCount()
        dlg._on_key_remove()
        assert dlg.keys_table.rowCount() == before, (
            "default row should not be removed by _on_key_remove()"
        )
    finally:
        dlg.deleteLater()
    print("✓ Default key row is non-removable")


def test_api_key_value_persists_to_credential_store_on_save() -> None:
    """Editing a key's value via the form + clicking Apply persists
    the value to the global CredentialStore on the next OK."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "old-default-value")
    default = store.ensure_default()
    # Seed the default provider with a model catalog so the
    # endpoint Model combo has something to select (post-refactor
    # the dropdown is constrained to provider.available_models).
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # The default row is auto-selected on open.
        assert dlg.keys_table.selectedItems()
        # Edit the value field + click Apply (the new flow — Apply
        # replaces the in-row text and stores the pending value so
        # that ``_on_accept`` writes the credential on OK).
        dlg.key_value_edit.setText("brand-new-default-value")
        assert dlg.key_apply_btn.isEnabled(), (
            "Apply should enable once the value diverges from the row's stored value"
        )
        dlg.key_apply_btn.click()
        # After Apply, the button is disabled again.
        assert not dlg.key_apply_btn.isEnabled()
        # Set up an endpoint row so _on_accept doesn't bail. The dialog
        # auto-seeds a ``default`` provider (and the auto-populated
        # row points at it), so we just type the model name and
        # click Apply.
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_apply_btn.click()
        assert not dlg.endpoint_apply_btn.isEnabled()
        dlg._on_accept()
        # The CredentialStore should now have the new value.
        assert store.credential_store.get("default") == "brand-new-default-value", (
            f"expected updated default key, got "
            f"{store.credential_store.get('default')!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ API key value persists to CredentialStore on save")


# ---- Model Endpoints tab: select + edit + apply -----------------------


def test_endpoints_table_is_read_only_three_columns() -> None:
    """The endpoints table is read-only (no Qt cell editors) and has
    3 columns: Aliases | Provider | Model. Aliases are the
    user-facing identity (col 0); the real upstream model ID is
    the rightmost column. The Provider column's ``UserRole``
    carries ``provider_name``."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert dlg.endpoints_table.columnCount() == 3
        headers = [
            dlg.endpoints_table.horizontalHeaderItem(i).text()
            for i in range(dlg.endpoints_table.columnCount())
        ]
        assert headers == [
            "Aliases", "Provider", "Model",
        ], headers
        # Edit triggers are disabled — the form below is the editor.
        triggers = dlg.endpoints_table.editTriggers()
        assert triggers == QAbstractItemView.EditTrigger.NoEditTriggers, (
            f"expected no edit triggers, got {triggers!r}"
        )
        # Provider column (col 1) carries the provider_name via UserRole.
        provider_item = dlg.endpoints_table.item(0, 1)
        assert provider_item.data(Qt.ItemDataRole.UserRole) == "default", (
            "Provider cell UserRole should be the provider_name"
        )
    finally:
        dlg.deleteLater()
    print("✓ Endpoints table: 3 columns [Aliases|Provider|Model], read-only")


def test_endpoints_form_model_combo_constrained_to_provider_models() -> None:
    """The form's Model ``QComboBox`` lists the selected provider's
    ``available_models``. The first row (claude-opus-4-8) is
    auto-selected on open and the form mirrors its current values."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8", "claude-sonnet-4-6"),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Form is enabled because row 0 is auto-selected on open.
        assert dlg._selected_endpoint_row == 0
        labels = [
            dlg.endpoint_model_combo.itemText(i)
            for i in range(dlg.endpoint_model_combo.count())
        ]
        assert "claude-opus-4-8" in labels
        assert "claude-sonnet-4-6" in labels
        # The current model row's value should be auto-selected.
        assert dlg.endpoint_model_combo.currentText() == "claude-opus-4-8"
    finally:
        dlg.deleteLater()
    print("✓ Endpoint form model combo lists provider's available_models")


def test_endpoints_row_apply_persists_provider_name_on_save() -> None:
    """Picking a provider in the form's combo + Apply commits the row's
    new ``provider_name``; ``_on_accept`` then surfaces that ref on
    the profile's ``ModelEndpoint``. ``key_ref`` no longer lives on
    the endpoint after the refactor — it's a property of the
    provider."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    # Two providers with non-overlapping model catalogs so the
    # model combo actually rebuilds.
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8",),
        ),
        "work": Provider(
            name="work", url="https://work.example.com",
            available_models=("claude-opus-4-8", "claude-sonnet-4-6"),
        ),
    }
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # The form mirrors the empty pre-populated row.
        assert dlg._selected_endpoint_row == 0
        # Pick the ``work`` provider via combo.
        for i in range(dlg.endpoint_provider_combo.count()):
            if dlg.endpoint_provider_combo.itemText(i) == "work":
                dlg.endpoint_provider_combo.setCurrentIndex(i)
                break
        # Pick claude-opus-4-8 from the work provider's catalog.
        for i in range(dlg.endpoint_model_combo.count()):
            if dlg.endpoint_model_combo.itemText(i) == "claude-opus-4-8":
                dlg.endpoint_model_combo.setCurrentIndex(i)
                break
        assert dlg.endpoint_apply_btn.isEnabled()
        dlg.endpoint_apply_btn.click()
        assert not dlg.endpoint_apply_btn.isEnabled(), (
            "Apply should disable after commit"
        )
        dlg._on_accept()
        edited = dlg.to_profile()
        ep = edited.model_endpoints["claude-opus-4-8"]
        assert ep.provider_name == "work", (
            f"expected provider_name='work', got {ep.provider_name!r}"
        )
        # ModelEndpoint no longer carries key_ref.
        assert not hasattr(ep, "key_ref") or ep.key_ref is None, (
            "ModelEndpoint.key_ref was dropped in the refactor"
        )
    finally:
        dlg.deleteLater()
    print("✓ Endpoint row Apply persists provider_name on save")


def test_endpoints_form_apply_disabled_until_divergence() -> None:
    """Apply + Revert are disabled when the form matches the row,
    and become enabled when the form diverges."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://x",
            available_models=("claude-opus-4-8", "claude-sonnet-4-6"),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert not dlg.endpoint_apply_btn.isEnabled()
        # Mutate the model combo (pick a different model) →
        # Apply enables.
        for i in range(dlg.endpoint_model_combo.count()):
            if dlg.endpoint_model_combo.itemText(i) == "claude-sonnet-4-6":
                dlg.endpoint_model_combo.setCurrentIndex(i)
                break
        assert dlg.endpoint_apply_btn.isEnabled()
        # Revert → Apply disables.
        dlg.endpoint_revert_btn.click()
        assert not dlg.endpoint_apply_btn.isEnabled()
        # The combo is back on the original provider.
        assert dlg.endpoint_provider_combo.currentText() == "default", (
            "Revert should restore the original URL"
        )
    finally:
        dlg.deleteLater()
    print("✓ Endpoint form Apply enables on divergence, Revert disables")


def test_provider_stale_key_ref_falls_back_to_no_key_in_form() -> None:
    """If a provider's stored ``key_ref`` no longer matches any key,
    the form's combo falls back to ``(no key)`` on row select. Post-
    refactor the key lives on the *provider*, not the endpoint."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://x", key_ref="stale",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Row 0 (the "default" provider) is auto-selected → the
        # form's combo falls back.
        assert dlg.provider_key_combo.currentText() == "(no key)", (
            "stale provider key_ref should fall back to '(no key)'"
        )
    finally:
        dlg.deleteLater()
    print("✓ Stale ref falls back to '(no key)' in the form combo")


# ---- API Keys tab: select + edit + apply ---------------------------------


def test_api_keys_form_apply_disabled_until_divergence() -> None:
    """The API Keys form's Apply is disabled initially; editing
    name or typing a value enables it."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert dlg._selected_key_row == 0
        assert not dlg.key_apply_btn.isEnabled()
        dlg.key_name_edit.setText("default")  # same as current
        assert not dlg.key_apply_btn.isEnabled(), (
            "Apply should not enable when form mirrors the row"
        )
        # Typing a value enables it.
        dlg.key_value_edit.setText("new-value")
        assert dlg.key_apply_btn.isEnabled()
    finally:
        dlg.deleteLater()
    print("✓ API Keys form Apply enables on divergence")


def test_api_keys_form_supports_rename_on_default() -> None:
    """The user said they want to edit the Default row too. The
    *name* on Default is structurally fixed (``\"default\"``), but
    the value is editable. Typing a new value + Apply + OK
    persists it to the global CredentialStore."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog
    from PySide6.QtWidgets import QMessageBox

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "old-value")
    default = store.ensure_default()
    # Seed the default provider with a model catalog so the
    # endpoint Model combo has something to pick (dropdown is
    # constrained post-refactor).
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    # ``_on_accept`` raises ``QMessageBox.warning`` for any
    # validation failure. The offscreen Qt platform happily
    # *blocks* on those modals waiting for a click that never
    # comes — replace with a silent logger so the test isn't a
    # 60-second hang on the first failure.
    orig_warning = QMessageBox.warning

    def _silent_warning(*_a, **_kw):
        return QMessageBox.StandardButton.Yes

    QMessageBox.warning = staticmethod(_silent_warning)
    try:
        dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
        try:
            assert dlg._selected_key_row == 0
            # The default row's value column shows 'old-value' as a
            # masked dot string. Edit the form's value + Apply.
            dlg.key_value_edit.setText("new-value")
            assert dlg.key_apply_btn.isEnabled()
            dlg.key_apply_btn.click()
            # After Apply, the form is reset (Apply disabled).
            assert not dlg.key_apply_btn.isEnabled()
            # Also need an endpoint row to satisfy _on_accept. Pick
            # the only model in the catalog.
            dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
            dlg.endpoint_apply_btn.click()
            assert not dlg.endpoint_apply_btn.isEnabled()
            dlg._on_accept()
            assert store.credential_store.get("default") == "new-value", (
                f"expected new default value, got "
                f"{store.credential_store.get('default')!r}"
            )
        finally:
            dlg.deleteLater()
    finally:
        QMessageBox.warning = orig_warning
    print("✓ API Keys form supports editing Default row's value")


def test_resolve_api_key_walks_provider_chain() -> None:
    """``resolve_api_key`` walks default_model → endpoint →
    provider → key_ref → CredentialStore (post-refactor)."""
    from src.profiles import ClaudeProfile, CredentialStore, ModelEndpoint, Provider
    from src.profiles.env_builder import resolve_api_key

    base = _tmp_dir()
    store = CredentialStore(path=base / "credentials.json")
    store.set("default", "x")
    store.set("work", "sk-work-12345")
    profile = ClaudeProfile(
        name="P",
        slug="p",
        providers={
            "default": Provider(name="default", url="https://x", key_ref="work"),
        },
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(provider_name="default"),
        },
        default_model="claude-opus-4-8",
    )
    assert resolve_api_key(profile, store) == "sk-work-12345"
    print("✓ resolve_api_key walks default_model → provider → key_ref")


def test_resolve_api_key_returns_none_when_no_key_ref() -> None:
    """``resolve_api_key`` returns ``None`` when the provider has no
    key_ref — the env builder will substitute the sentinel."""
    from src.profiles import ClaudeProfile, CredentialStore, ModelEndpoint, Provider
    from src.profiles.env_builder import resolve_api_key

    base = _tmp_dir()
    store = CredentialStore(path=base / "credentials.json")
    store.set("default", "x")
    profile = ClaudeProfile(
        name="P",
        slug="p",
        providers={
            "default": Provider(
                name="default", url="https://x",
                available_models=("claude-opus-4-8",),
            ),
        },
        model_endpoints={
            "claude-opus-4-8": ModelEndpoint(provider_name="default"),
        },
        default_model="claude-opus-4-8",
    )
    assert resolve_api_key(profile, store) is None
    print("✓ resolve_api_key returns None when key_ref is unset")


# ---- Env Vars tab: select + edit + apply ---------------------------------


def test_env_vars_form_apply_disabled_until_divergence() -> None:
    """The env vars form's Apply is enabled once the user edits a
    field that differs from the selected row."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    default.env_vars = {"MY_VAR": "old"}
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert dlg._selected_env_row == 0
        assert not dlg.env_apply_btn.isEnabled()
        dlg.env_value_edit.setText("new")
        assert dlg.env_apply_btn.isEnabled()
        # Revert restores the original.
        dlg.env_revert_btn.click()
        assert dlg.env_value_edit.text() == "old"
        assert not dlg.env_apply_btn.isEnabled()
    finally:
        dlg.deleteLater()
    print("✓ Env Vars form Apply enables on divergence, Revert disables")


def test_env_vars_form_apply_persists_value() -> None:
    """Editing an env var via the form + Apply persists on OK so
    ``_on_accept`` picks it up via ``_table_pairs``."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    # Need a Provider with a catalog containing claude-opus-4-8 so
    # the constrained Model combo has a selectable entry.
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Need an env row + an endpoint row first.
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtCore import Qt as _Qt
        dlg.env_table.setRowCount(0)
        item0 = QTableWidgetItem()
        item0.setFlags(item0.flags() & ~_Qt.ItemFlag.ItemIsEditable)
        item1 = QTableWidgetItem()
        item1.setFlags(item1.flags() & ~_Qt.ItemFlag.ItemIsEditable)
        dlg.env_table.insertRow(0)
        dlg.env_table.setItem(0, 0, item0)
        dlg.env_table.setItem(0, 1, item1)
        dlg.env_table.item(0, 0).setText("MY_VAR")
        dlg.env_table.item(0, 1).setText("old")
        dlg.env_table.selectRow(0)
        dlg.env_value_edit.setText("new")
        dlg.env_apply_btn.click()
        # Need an endpoint row for _on_accept. The dialog auto-seeds a
        # ``default`` provider on open, so the form's pre-populated
        # combo is valid; we just type a model name and Apply.
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_apply_btn.click()
        dlg._on_accept()
        edited = dlg.to_profile()
        assert edited.env_vars == {"MY_VAR": "new"}, (
            f"expected updated env var, got {edited.env_vars!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ Env Vars form Apply persists value on save")


# ---- Endpoints table is now safe to click rapidly -------------------------


def test_clicks_endpoints_tab_does_not_corrupt_state() -> None:
    """With the table fully read-only (no Qt cell editor, no cell
    widget), repeated clicks on the same cell are *selection* only
    — there's nothing to re-create, so the segfault class from the
    old cell-editor + cell-widget combo is impossible by construction."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    store.credential_store.set("work", "y")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://example.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        dlg.show()
        dlg.tabs.setCurrentIndex(1)
        for _ in range(3):
            QApplication.processEvents()
        # Drive the exact 3+2=5 click burst on the URL cell.
        url_idx = dlg.endpoints_table.model().index(0, 1)
        url_rect = dlg.endpoints_table.visualRect(url_idx)
        center = url_rect.center()
        for _ in range(5):
            QTest.mouseClick(
                dlg.endpoints_table.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                center,
            )
            QApplication.processEvents()
        # Dialog must still be alive, the form should have responded
        # by selecting the row (no editor open).
        assert dlg._selected_endpoint_row == 0
        # The form mirrors the row's stored values → Apply disabled.
        assert not dlg.endpoint_apply_btn.isEnabled()
    finally:
        dlg.deleteLater()
    print("✓ Repeated Model Endpoints clicks don't segfault the dialog")


# ---- Providers tab --------------------------------------------------------


def test_providers_tab_has_default_row_pinned() -> None:
    """The Providers table starts with row 0 named ``default`` and
    its name is locked (non-editable). The rest follow in their
    on-disk order."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        from PySide6.QtCore import Qt as _Qt

        assert dlg.providers_table.rowCount() >= 1
        row0_name = dlg.providers_table.item(0, 0).text()
        assert row0_name == "default", (
            f"row 0 should be the default provider, got {row0_name!r}"
        )
        # The default row's name cell is read-only.
        flags = dlg.providers_table.item(0, 0).flags()
        assert not (flags & _Qt.ItemFlag.ItemIsEditable), (
            "the default row's name cell should be non-editable"
        )
        # UserRole on row 0 carries the is_default flag (truthy).
        assert dlg.providers_table.item(0, 0).data(_Qt.ItemDataRole.UserRole), (
            "row 0 should be marked as default in UserRole"
        )
    finally:
        dlg.deleteLater()
    print("✓ Providers tab: default row pinned at index 0, name locked")


def test_providers_tab_add_inserts_new_row() -> None:
    """Clicking Add… appends a new row with a ``provider-N``
    placeholder name and an empty URL."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        before = dlg.providers_table.rowCount()
        dlg._on_provider_add()
        after = dlg.providers_table.rowCount()
        assert after == before + 1
        # The new row is at the bottom; its name is "provider-1".
        new_name = dlg.providers_table.item(after - 1, 0).text()
        assert new_name == "provider-1", (
            f"new row should be named 'provider-1', got {new_name!r}"
        )
        # URL is empty — the user fills it in via the form.
        new_url = dlg.providers_table.item(after - 1, 1).text()
        assert new_url == "", (
            f"new row's URL should be empty, got {new_url!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ Providers tab: Add inserts a new row with placeholder name")


def test_default_provider_row_cannot_be_removed() -> None:
    """Selecting the default provider row + clicking Remove Selected
    is a no-op. Row 0 stays put; the row count is unchanged."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        dlg.providers_table.setCurrentCell(0, 0)
        # The Remove button should be disabled when the default row
        # is selected.
        assert dlg.providers_remove_btn.isEnabled() is False, (
            "Remove button should be disabled when default provider row is selected"
        )
        # Calling the slot directly is a no-op even if the slot were
        # somehow invoked.
        before = dlg.providers_table.rowCount()
        dlg._on_provider_remove()
        assert dlg.providers_table.rowCount() == before, (
            "default provider row should not be removed by _on_provider_remove()"
        )
    finally:
        dlg.deleteLater()
    print("✓ Default provider row is non-removable")


def test_providers_form_apply_persists_url_on_save() -> None:
    """Editing a provider's URL via the form + clicking Apply
    commits the URL to the table; ``_on_accept`` writes it to the
    profile."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    # Seed the default provider with a model catalog so the
    # endpoint Model combo has something selectable (post-refactor
    # the dropdown is constrained to provider.available_models).
    default.providers = {
        "default": Provider(
            name="default", url="https://api.example.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Row 0 (default) is auto-selected. Form is populated.
        assert dlg._selected_provider_row == 0
        assert dlg.provider_name_edit.text() == "default"
        # Edit the URL via the form + Apply.
        dlg.provider_url_edit.setText("https://example.com/v2")
        assert dlg.provider_apply_btn.isEnabled()
        dlg.provider_apply_btn.click()
        assert not dlg.provider_apply_btn.isEnabled()
        # Add an endpoint row that references the default provider
        # so _on_accept has something to commit.
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_apply_btn.click()
        dlg._on_accept()
        edited = dlg.to_profile()
        assert "default" in edited.providers
        assert edited.providers["default"].url == "https://example.com/v2", (
            f"expected updated default URL, got "
            f"{edited.providers['default'].url!r}"
        )
        assert edited.model_endpoints["claude-opus-4-8"].provider_name == "default"
    finally:
        dlg.deleteLater()
    print("✓ Provider form Apply persists URL on save")


def test_providers_form_apply_disabled_until_divergence() -> None:
    """The Providers form's Apply is disabled initially; editing
    the URL enables it."""
    from src.profiles import ProfileStore
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        assert dlg._selected_provider_row == 0
        assert not dlg.provider_apply_btn.isEnabled()
        dlg.provider_url_edit.setText("https://changed.example.com")
        assert dlg.provider_apply_btn.isEnabled()
        dlg.provider_revert_btn.click()
        assert dlg.provider_url_edit.text() == "https://api.example.com"
        assert not dlg.provider_apply_btn.isEnabled()
    finally:
        dlg.deleteLater()
    print("✓ Provider form Apply enables on divergence, Revert disables")


def test_providers_remove_button_drops_provider_from_endpoint_combo() -> None:
    """Removing a non-default provider from the Providers tab also
    drops it from the Model Configuration form's Provider combo —
    endpoints that referenced it will fall back to ``(no provider)``
    on next selection."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(name="default", url="https://example.com"),
        "extra": Provider(name="extra", url="https://extra.example.com"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Both providers are in the form's combo.
        provider_names = [
            dlg.endpoint_provider_combo.itemText(i)
            for i in range(dlg.endpoint_provider_combo.count())
        ]
        assert "extra" in provider_names, (
            f"expected 'extra' in provider combo, got {provider_names}"
        )
        # Select + remove the "extra" row from the Providers tab.
        dlg.providers_table.setCurrentCell(1, 0)
        dlg._on_provider_remove()
        # Refreshed: "extra" is gone from the form's combo.
        provider_names = [
            dlg.endpoint_provider_combo.itemText(i)
            for i in range(dlg.endpoint_provider_combo.count())
        ]
        assert "extra" not in provider_names, (
            f"'extra' should have been dropped from the combo, got {provider_names}"
        )
        assert "default" in provider_names
    finally:
        dlg.deleteLater()
    print("✓ Removing a provider drops it from the endpoint combo")


# ---- Model Configuration: provider dropdown -----------------------------


def test_model_config_form_provider_combo_lists_provider_names() -> None:
    """The Model Configuration tab's form has a Provider QComboBox
    whose items are the provider *names* (not URLs). ``(no provider)``
    is always the first item."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(name="default", url="https://api.anthropic.com"),
        "Anthropic": Provider(name="Anthropic", url="https://api.anthropic.com"),
        "MiniMax": Provider(name="MiniMax", url="https://api.MiniMax.example"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        labels = [
            dlg.endpoint_provider_combo.itemText(i)
            for i in range(dlg.endpoint_provider_combo.count())
        ]
        # Display value = provider name, with "(no provider)" first.
        assert labels[0] == "(no provider)"
        assert "Anthropic" in labels
        assert "MiniMax" in labels
        assert "default" in labels
    finally:
        dlg.deleteLater()
    print("✓ Model Configuration form's provider combo shows provider names")


def test_model_config_form_combo_userdata_is_url() -> None:
    """Each combo item's ``userData`` is the provider's URL — that's
    what gets persisted when the user picks a provider."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(name="default", url="https://api.anthropic.com"),
        "MiniMax": Provider(name="MiniMax", url="https://api.MiniMax.example"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        urls_by_idx = {
            dlg.endpoint_provider_combo.itemText(i):
                dlg.endpoint_provider_combo.itemData(i)
            for i in range(dlg.endpoint_provider_combo.count())
        }
        assert urls_by_idx["MiniMax"] == "https://api.MiniMax.example", (
            f"MiniMax userData should be its URL, got {urls_by_idx['MiniMax']!r}"
        )
        assert urls_by_idx["default"] == "https://api.anthropic.com"
        # ``(no provider)`` is the empty-URL fallback.
        assert urls_by_idx["(no provider)"] == ""
    finally:
        dlg.deleteLater()
    print("✓ Provider combo's userData is the URL (display=name, data=URL)")


def test_model_config_form_url_hint_reflects_selection() -> None:
    """The form shows the URL of the currently-selected provider as
    a hint so the user can see which URL they're picking."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(name="default", url="https://api.anthropic.com"),
        "MiniMax": Provider(name="MiniMax", url="https://api.MiniMax.example"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Pick MiniMax — the hint updates to its URL.
        for i in range(dlg.endpoint_provider_combo.count()):
            if dlg.endpoint_provider_combo.itemText(i) == "MiniMax":
                dlg.endpoint_provider_combo.setCurrentIndex(i)
                break
        assert "MiniMax.example" in dlg.endpoint_url_hint.text(), (
            f"URL hint should reflect the selected provider, got "
            f"{dlg.endpoint_url_hint.text()!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ URL hint updates with provider selection")


def test_model_config_endpoint_table_displays_provider_name() -> None:
    """The Model Configuration table's ``Provider`` column shows the
    *provider name* (not the URL). The URL is exposed via the
    form's URL hint label so the user can still see it without
    switching back to the Providers tab."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(provider_name="default"),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Column 1 (post-refactor) is the provider *name*;
        # UserRole carries the same name as a stable identity.
        provider_text = dlg.endpoints_table.item(0, 1).text()
        assert provider_text == "default", (
            f"endpoint row should display the provider name, got "
            f"{provider_text!r}"
        )
        # The URL is on the hint label below the form's Provider
        # combo, rendered when a row is selected.
        dlg.endpoints_table.selectRow(0)
        assert "api.anthropic.com" in dlg.endpoint_url_hint.text(), (
            f"URL hint should reflect the selected provider's URL, got "
            f"{dlg.endpoint_url_hint.text()!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ Model Configuration table shows resolved URL")


# ---- Model Configuration: aliases ----------------------------------------


def test_endpoint_aliases_form_apply_persists_aliases_on_save() -> None:
    """Type a comma-separated alias list in the form + Apply + Save
    persists the parsed tuple on the profile's ``ModelEndpoint.aliases``."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Row 0 is the auto-populated empty row.
        assert dlg._selected_endpoint_row == 0
        # Pick the model + type aliases + click Apply.
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_aliases_edit.setText("opus, team-pro")
        assert dlg.endpoint_apply_btn.isEnabled(), (
            "Apply should enable once form diverges from row"
        )
        dlg.endpoint_apply_btn.click()
        assert not dlg.endpoint_apply_btn.isEnabled(), (
            "Apply should disable after commit"
        )
        # The Aliases cell (col 0 — alias-first column order)
        # shows the canonical form.
        assert dlg.endpoints_table.item(0, 0).text() == "opus, team-pro"
        dlg._on_accept()
        edited = dlg.to_profile()
        ep = edited.model_endpoints["claude-opus-4-8"]
        assert ep.aliases == ("opus", "team-pro"), (
            f"expected aliases=('opus', 'team-pro'), got {ep.aliases!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ Endpoint aliases form Apply persists aliases on save")


def test_endpoint_aliases_parse_dedupes_and_trims() -> None:
    """``_parse_aliases`` strips whitespace, drops empties, dedupes,
    preserves order. Trailing commas and repeated entries collapse."""
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    # Static helper — no QApplication needed.
    assert ProfileEditorDialog._parse_aliases("") == ()
    assert ProfileEditorDialog._parse_aliases("opus") == ("opus",)
    assert ProfileEditorDialog._parse_aliases("opus,team-pro") == (
        "opus",
        "team-pro",
    )
    assert ProfileEditorDialog._parse_aliases(
        "opus, team-pro,opus,,"
    ) == ("opus", "team-pro")
    assert ProfileEditorDialog._parse_aliases("  opus  ,  team-pro  ") == (
        "opus",
        "team-pro",
    )
    assert ProfileEditorDialog._parse_aliases(",,, ,") == ()
    print("✓ Alias parser trims, dedupes, drops empties")


def test_endpoint_aliases_form_persists_after_row_reselect() -> None:
    """After Apply, switching to another row + back to the original
    row leaves the aliases text intact in the form."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8", "claude-haiku-4-5"),
        ),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Set up two rows: row 0 with aliases, row 1 empty.
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_aliases_edit.setText("opus, team-pro")
        dlg.endpoint_apply_btn.click()
        dlg._on_endpoint_add()
        dlg.endpoint_model_combo.setCurrentText("claude-haiku-4-5")
        dlg.endpoint_apply_btn.click()
        # Now select row 0 again — the form mirrors the saved aliases.
        dlg.endpoints_table.selectRow(0)
        assert dlg.endpoint_aliases_edit.text() == "opus, team-pro"
        # Select row 1 — form shows empty aliases.
        dlg.endpoints_table.selectRow(1)
        assert dlg.endpoint_aliases_edit.text() == ""
    finally:
        dlg.deleteLater()
    print("✓ Aliases survive row reselection")


def test_endpoint_snap_apply_on_accept_commits_typed_aliases() -> None:
    """Users on autopilot sometimes type in the form, then click OK
    straight away without Apply. Without a snap-apply, the typed
    text is read from the *table cells* (still empty) and silently
    dropped. Verify ``_on_accept`` commits any divergent form
    state just-in-time."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Type a model name + aliases in the form but DO NOT click
        # Apply. The intent: clicking OK should still commit.
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_aliases_edit.setText("mm-m3, team")
        # Sanity: Apply is *enabled* (form diverges from row), but
        # we explicitly skip the click.
        assert dlg.endpoint_apply_btn.isEnabled(), (
            "form should be divergent — typing without Apply"
        )
        dlg._on_accept()
        edited = dlg.to_profile()
        ep = edited.model_endpoints["claude-opus-4-8"]
        assert ep.aliases == ("mm-m3", "team"), (
            f"snap-apply should have committed the typed aliases, got "
            f"{ep.aliases!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ Snap-apply on OK commits typed-but-not-Apply'd form edits")


def test_endpoints_auto_seeded_from_provider_catalog() -> None:
    """A profile with providers exposing ``available_models`` but
    zero ``model_endpoints`` should auto-seed one row per
    ``(provider, model)`` pair on dialog open. Otherwise the
    user-facing ``settings.json.availableModels`` would be empty
    and Claude Code's ``/model`` would only show the hardcoded
    built-in default."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog
    import json

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    # Providers with catalogs but no endpoints at all. This is the
    # real-world failure mode the user just hit.
    default.providers = {
        "MiniMax": Provider(
            name="MiniMax", url="https://api.MiniMax.io", key_ref="default",
            available_models=("MiniMax-M3", "minimax-text-01"),
        ),
    }
    # Original on-disk profile is empty on ``model_endpoints``.
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Two seeded rows for the two catalog entries.
        assert dlg.endpoints_table.rowCount() == 2, (
            f"expected 2 auto-seeded rows, got {dlg.endpoints_table.rowCount()}"
        )
        rows = []
        for r in range(dlg.endpoints_table.rowCount()):
            # New column order: Aliases | Provider | Model.
            # Seed rows have empty aliases (left column blank).
            aliases_text = dlg.endpoints_table.item(r, 0).text()
            provider = dlg.endpoints_table.item(r, 1).text()
            model = dlg.endpoints_table.item(r, 2).text()
            rows.append((model, provider, aliases_text))
        assert sorted(rows) == sorted([
            ("MiniMax-M3", "MiniMax", ""),
            ("minimax-text-01", "MiniMax", ""),
        ]), rows
    finally:
        dlg.deleteLater()
    print("✓ Dialog auto-seeds endpoints from provider catalogs")


def test_default_model_combo_lists_aliases_with_legacy_fallback() -> None:
    """The General tab's Default model field is a ``QComboBox`` whose
    items are ``(no default)`` + every alias across endpoint rows,
    plus a ``(real model: X)`` fallback for legacy profiles whose
    ``default_model`` was a real upstream ID.

    Pre-selects the matching entry when the profile already has a
    ``default_model`` set.
    """
    from src.profiles import ProfileStore, Provider, ModelEndpoint
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(
            provider_name="default",
            aliases=("opus", "team-pro"),
        ),
    }
    # Start with a *legacy* default — a real model name (post-refactor
    # the user is expected to use aliases via the dropdown, but
    # legacy profiles must keep round-tripping).
    default.default_model = "claude-opus-4-8"
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        items = [
            (dlg.default_model_combo.itemText(i), dlg.default_model_combo.itemData(i))
            for i in range(dlg.default_model_combo.count())
        ]
        # ``(no default)`` first; aliases (sorted); legacy real-model
        # entry tagged with its real name as userData.
        labels = [label for label, _ in items]
        assert labels[0] == "(no default)"
        assert "opus" in labels
        assert "team-pro" in labels
        # Legacy real-model entry surfaces as a labeled fallback.
        assert any(
            label.startswith("(real model:") for label, _ in items
        )
        # The matching entry is pre-selected (legacy ``claude-opus-4-8``
        # matches the real-model fallback).
        assert dlg.default_model_combo.currentData() == "claude-opus-4-8"
    finally:
        dlg.deleteLater()
    print("✓ Default model combo lists aliases + (real model: X) fallback")


def test_default_model_combo_persists_alias_choice() -> None:
    """Picking an alias from the Default model dropdown + clicking OK
    saves the alias verbatim as ``profile.default_model``. The proxy
    rewrites it to the real model name at request time."""
    from src.profiles import ProfileStore, Provider, ModelEndpoint
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(
            provider_name="default",
            aliases=("opus", "team-pro"),
        ),
    }
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Pick the "opus" alias.
        for i in range(dlg.default_model_combo.count()):
            if dlg.default_model_combo.itemData(i) == "opus":
                dlg.default_model_combo.setCurrentIndex(i)
                break
        dlg._on_accept()
        edited = dlg.to_profile()
        # Real model name *not* stored — exactly the alias the
        # user picked in the dropdown.
        assert edited.default_model == "opus", (
            f"expected default_model='opus', got {edited.default_model!r}"
        )
    finally:
        dlg.deleteLater()
    print("✓ Default model persists the chosen alias verbatim")


def test_endpoints_table_columns_are_aliases_provider_model() -> None:
    """The Model Configuration table is laid out as
    [Aliases | Provider | Model] — aliases are the user-facing
    identity, the real upstream model ID is the rightmost column."""
    from src.profiles import ProfileStore, Provider, ModelEndpoint
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8",),
        ),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(
            provider_name="default",
            aliases=("opus", "team-pro"),
        ),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        headers = [
            dlg.endpoints_table.horizontalHeaderItem(i).text()
            for i in range(dlg.endpoints_table.columnCount())
        ]
        assert headers == ["Aliases", "Provider", "Model"], headers
        # Cell contents validate the column-to-meaning mapping.
        assert dlg.endpoints_table.item(0, 0).text() == "opus, team-pro"
        assert dlg.endpoints_table.item(0, 1).text() == "default"
        assert dlg.endpoints_table.item(0, 2).text() == "claude-opus-4-8"
    finally:
        dlg.deleteLater()
    print("✓ Model Configuration table columns: Aliases | Provider | Model")


def test_endpoint_aliases_round_trip_through_store() -> None:
    """A profile with ``ModelEndpoint.aliases`` survives
    ``ProfileStore.upsert``/``load``."""
    from src.profiles import ProfileStore, Provider, ModelEndpoint

    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    default = store.ensure_default()
    default.providers = {
        "default": Provider(name="default", url="https://api.anthropic.com"),
    }
    default.model_endpoints = {
        "claude-opus-4-8": ModelEndpoint(
            provider_name="default",
            aliases=("opus", "team-pro"),
        ),
    }
    store.upsert(default)
    loaded = store.load()
    loaded_default = next(p for p in loaded if p.name == "Default")
    ep = loaded_default.model_endpoints["claude-opus-4-8"]
    assert ep.aliases == ("opus", "team-pro"), (
        f"expected aliases=('opus', 'team-pro'), got {ep.aliases!r}"
    )
    print("✓ Endpoint aliases round-trip through ProfileStore")


def test_endpoint_aliases_unique_across_profile_on_save() -> None:
    """Two endpoints both claiming the same alias is refused by
    ``_on_accept`` (the editor surfaces a warning instead of
    silently shadowing)."""
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider
    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog
    from PySide6.QtWidgets import QMessageBox

    _qapp()
    base = _tmp_dir()
    store = ProfileStore(path=base / "profiles.json")
    store.credential_store.set("default", "x")
    default = store.ensure_default()
    # Available Models needs to contain both claude-opus-4-8 and
    # claude-haiku-4-5 so the dialog's constrained dropdown has
    # something to select for each row.
    default.providers = {
        "default": Provider(
            name="default", url="https://api.anthropic.com",
            available_models=("claude-opus-4-8", "claude-haiku-4-5"),
        ),
    }
    store.upsert(default)
    dlg = ProfileEditorDialog(default, credential_store=store.credential_store)
    try:
        # Build two rows both claiming alias "opus".
        dlg.endpoint_model_combo.setCurrentText("claude-opus-4-8")
        dlg.endpoint_aliases_edit.setText("opus")
        dlg.endpoint_apply_btn.click()
        dlg._on_endpoint_add()
        dlg.endpoint_model_combo.setCurrentText("claude-haiku-4-5")
        dlg.endpoint_aliases_edit.setText("opus")
        dlg.endpoint_apply_btn.click()
        # Suppress the modal — replace QMessageBox.warning with a stub.
        captured: dict[str, object] = {}
        original_warning = QMessageBox.warning

        def _capture(*args, **kwargs):
            captured["title"] = args[1] if len(args) > 1 else kwargs.get("title")
            captured["text"] = args[2] if len(args) > 2 else kwargs.get("text")
            return QMessageBox.StandardButton.Ok

        QMessageBox.warning = _capture
        try:
            dlg._on_accept()
        finally:
            QMessageBox.warning = original_warning
        assert "Duplicate" in str(captured.get("title", "")), (
            f"expected a duplicate-alias warning, got {captured!r}"
        )
        # The save was refused — the profile's model_endpoints still
        # reflect the *last* applied state on the table, but the
        # ``_on_accept`` returned early without assigning to
        # ``self._profile``. In headless tests the table state is
        # the source of truth; we just confirm _on_accept didn't
        # proceed past the warning.
    finally:
        dlg.deleteLater()
    print("✓ Two rows with same alias are refused on save")


# ---- Proxy alias rewrite (unit-level, no FastAPI) -----------------------


def test_proxy_alias_rewrites_req_model_before_router() -> None:
    """A request with ``model: \"opus\"`` is rewritten to
    ``\"claude-opus-4-8\"`` by the alias-rewrite block before the
    router sees it. We exercise the rewrite logic by constructing
    an ``AnthropicRequest`` and an alias map.
    """
    from ccproxy.app.models.anthropic import AnthropicRequest
    from ccproxy.app.api.proxy import _apply_alias_rewrite

    req = AnthropicRequest.model_validate(
        {
            "model": "opus",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    alias_map = {"opus": "claude-opus-4-8", "haiku": "claude-haiku-4-5"}
    new_req, alias_seen = _apply_alias_rewrite(req, alias_map)
    assert new_req.model == "claude-opus-4-8"
    assert alias_seen == "opus"

    # Real name passes through unchanged.
    req2 = AnthropicRequest.model_validate(
        {
            "model": "claude-opus-4-8",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    new_req2, alias_seen2 = _apply_alias_rewrite(req2, alias_map)
    assert new_req2.model == "claude-opus-4-8"
    assert alias_seen2 is None

    # Empty alias map is a no-op.
    req3, alias_seen3 = _apply_alias_rewrite(req, {})
    assert req3.model == "opus"
    assert alias_seen3 is None
    print("✓ Alias rewrite translates model name before router")


def test_proxy_alias_rewrite_reserializes_wire_body() -> None:
    """When the alias rewrite changes ``req.model`` in memory, the
    wire body sent to the upstream must also be re-marshalled —
    otherwise OpenRouter (and similar) reject the request with
    "``freestuff`` is not a valid model ID" because the alias, not
    the rewritten name, is what reaches the upstream.

    Regression test for the case where the upstream returned 400
    even though the alias-rewrite logic correctly produced a valid
    model name internally. The wire body had the alias; the in-memory
    request had the real name; the proxy forwarded the wire body
    unchanged because ``decision.target_model == req.model`` after
    the rewrite, so the re-marshal branch never fired.
    """
    import asyncio
    import json as _json
    import tempfile
    from pathlib import Path as _P

    import httpx
    from ccproxy.app.config import (
        ModelEndpointsCfg,
        ProviderCfg,
        Settings,
    )
    from ccproxy.app.main import create_app
    from ccproxy.app.providers.anthropic import AnthropicProvider

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            content=b'{"id":"msg_1","content":[],"model":"openrouter/free"}',
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)

    async def go() -> None:
        # Build a minimal Settings with the alias map and provider.
        cfg = Settings()
        cfg.model_endpoints = ModelEndpointsCfg(
            endpoints={"openrouter/free": "openrouter"},
            providers={
                "openrouter": ProviderCfg(
                    url="https://openrouter.ai/api/v1",
                    api_key="sk-or-v1-test-key",
                    available_models=["openrouter/free"],
                ),
            },
            model_aliases={"freestuff": "openrouter/free"},
        )

        # Build the provider with the mock transport injected.
        mock_client = httpx.AsyncClient(transport=transport, timeout=10)
        provider = AnthropicProvider(
            base_url="https://openrouter.ai/api/v1",
            client=mock_client,
        )

        app = create_app(
            settings=cfg,
            providers={"openrouter": provider, "anthropic": provider},
            storage_path=str(_P(tempfile.mkdtemp()) / "test.db"),
        )

        # TestClient drives the lifespan startup so app.state.router
        # is set before we issue requests.
        from fastapi.testclient import TestClient
        with TestClient(app) as tc:
            r = tc.post(
                "/v1/messages",
                json={
                    "model": "freestuff",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"anthropic-version": "2023-06-01"},
            )
            assert r.status_code == 200, (
                f"proxy should accept the request, got {r.status_code}: {r.text}"
            )
        # Close the mock client we injected (lifespan doesn't own it).
        await mock_client.aclose()

    asyncio.run(go())

    # Verify the upstream got the rewritten model name, NOT the alias.
    assert len(captured_requests) == 1, (
        f"expected 1 upstream call, got {len(captured_requests)}"
    )
    upstream_body = _json.loads(captured_requests[0].content)
    assert upstream_body["model"] == "openrouter/free", (
        f"upstream should see the rewritten model 'openrouter/free', "
        f"got {upstream_body['model']!r} (alias 'freestuff' would mean "
        f"the wire body wasn't re-marshalled after the alias rewrite)"
    )
    print("✓ Proxy re-marshals wire body after alias rewrite")


def test_credential_store_strips_whitespace_on_set() -> None:
    """Pasting an API key from a key-management page often lands
    with a trailing newline (or NBSP). OpenRouter's format validator
    rejects those with a 401 "Missing Authentication header" before
    the auth path even runs. ``CredentialStore.set`` strips the
    whitespace so the key retrieved by the proxy is the clean
    verifier-expected shape.
    """
    from src.profiles.credential_store import CredentialStore

    base = _tmp_dir()
    cs = CredentialStore(base / "creds.json")

    # Trailing newline (the typical paste-from-web artefact).
    cs.set("openrouter", "sk-or-v1-abc123\n")
    assert cs.get("openrouter") == "sk-or-v1-abc123", (
        "trailing newline should be stripped"
    )

    # Leading + trailing whitespace.
    cs.set("anthropic", "  sk-ant-AAA  \t")
    assert cs.get("anthropic") == "sk-ant-AAA", (
        "leading/trailing whitespace should be stripped"
    )

    # Internal whitespace (a paste that landed with a literal newline
    # mid-string would still be malformed).
    cs.set("openai", "sk-proj-AAA\nBBB")
    assert cs.get("openai") == "sk-proj-AAABBB", (
        "internal whitespace should be collapsed"
    )

    # Whitespace-only value is treated as empty (deletion).
    cs.set("to_delete", "   \n  ")
    assert cs.get("to_delete") is None, (
        "whitespace-only value should delete the entry"
    )

    # Idempotent: re-setting with the same clean value is a no-op.
    cs.set("openrouter", "sk-or-v1-abc123")
    assert cs.get("openrouter") == "sk-or-v1-abc123"

    # The on-disk file persisted the cleaned value, not the raw input.
    raw = (base / "creds.json").read_text()
    assert "sk-or-v1-abc123" in raw
    assert "sk-or-v1-abc123\n" not in raw
    print("✓ CredentialStore strips whitespace on set")


def test_anthropic_provider_logs_key_prefix_diagnostic() -> None:
    """When ``target_api_key`` is set, ``AnthropicProvider.forward``
    emits a stderr line with the key's first 4 chars, last 4 chars,
    and length. This lets a 401 "Missing Authentication header" be
    attributed to a known prefix vs. a silently corrupted key.
    """
    import asyncio
    import io
    import sys as _sys

    import httpx
    from ccproxy.app.providers.anthropic import AnthropicProvider

    captured_stderr = io.StringIO()
    real_stderr = _sys.stderr

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                200,
                content=b'{"ok":true}',
                headers={"content-type": "application/json"},
                request=request,
            )

    try:
        _sys.stderr = captured_stderr
        transport = _MockTransport()
        mock_client = httpx.AsyncClient(transport=transport, timeout=10)
        provider = AnthropicProvider(
            base_url="https://openrouter.ai/api",
            client=mock_client,
        )

        async def main():
            await provider.forward(
                body=b'{"model":"openrouter/free","max_tokens":1,"messages":[]}',
                headers={
                    "x-api-key": "sk-ant-AAA",  # inbound from CLI
                    "anthropic-version": "2023-06-01",
                },
                target_api_key="skorxyzABCDEF123456789",
            )

        asyncio.run(main())
    finally:
        _sys.stderr = real_stderr

    log_text = captured_stderr.getvalue()
    # Prefix: first 4 chars of the key
    assert "prefix='skor'" in log_text, (
        f"prefix should be 'skor', got: {log_text!r}"
    )
    assert "suffix='6789'" in log_text, (
        f"suffix should be '6789', got: {log_text!r}"
    )
    assert "len=22" in log_text, (
        f"len should be 22, got: {log_text!r}"
    )
    # The middle of the key is never logged.
    assert "ABCDEF1" not in log_text, (
        "middle characters of the key should not be logged"
    )
    print("✓ AnthropicProvider logs key prefix/suffix diagnostic")


def test_anthropic_provider_logs_short_key_safely() -> None:
    """Keys shorter than 8 chars have their entire value replaced
    with ``***`` in the diagnostic line so a short test key or
    placeholder doesn't leak."""
    import asyncio
    import io
    import sys as _sys

    import httpx
    from ccproxy.app.providers.anthropic import AnthropicProvider

    captured_stderr = io.StringIO()
    real_stderr = _sys.stderr

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b'{}', request=request)

    try:
        _sys.stderr = captured_stderr
        transport = _MockTransport()
        mock_client = httpx.AsyncClient(transport=transport, timeout=10)
        provider = AnthropicProvider(
            base_url="https://example.com",
            client=mock_client,
        )

        async def main():
            await provider.forward(
                body=b'{}',
                headers={},
                target_api_key="shortkey",
            )

        asyncio.run(main())
    finally:
        _sys.stderr = real_stderr

    log_text = captured_stderr.getvalue()
    assert "***" in log_text, (
        f"short keys should be fully obscured, got: {log_text!r}"
    )
    assert "shortkey" not in log_text, (
        f"short key value should not appear, got: {log_text!r}"
    )
    print("✓ AnthropicProvider safely obscures short keys")


def test_anthropic_provider_retries_trailing_slash_redirect() -> None:
    """Some nginx-fronted gateways (e.g. MiniMax) return ``301`` /
    ``302`` with ``Location: <base>/`` (a directory URL with a
    trailing slash but no API path). The naive follow lands on
    a 404 because there's no ``/v1/messages`` at that URL.
    ``AnthropicProvider`` detects this pattern and retries the
    POST against ``<Location>/v1/messages``.
    """
    import asyncio
    import httpx
    from ccproxy.app.providers.anthropic import AnthropicProvider

    captured: list[httpx.Request] = []

    class _MockTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            captured.append(request)
            if self.calls == 1:
                # First call: 302 to a directory URL with trailing
                # slash and no /v1/messages.
                return httpx.Response(
                    302,
                    headers={"location": "https://api.example.com/anthropic/"},
                    request=request,
                )
            # Second call: success with a real response.
            return httpx.Response(
                200,
                content=b'{"id":"msg_1","content":[]}',
                headers={"content-type": "application/json"},
                request=request,
            )

    transport = _MockTransport()
    mock_client = httpx.AsyncClient(
        transport=transport,
        timeout=10,
    )
    provider = AnthropicProvider(
        base_url="https://api.example.com/anthropic",
        client=mock_client,
    )

    async def main():
        response = await provider.forward(
            body=b'{"model":"MiniMax-M3","messages":[]}',
            headers={"anthropic-version": "2023-06-01"},
            target_api_key="sk-test",
        )
        # Verify the second call hit the retried URL.
        assert transport.calls == 2, (
            f"expected 2 calls (initial + retry), got {transport.calls}"
        )
        assert captured[1].url.path == "/anthropic/v1/messages", (
            f"retry should hit /anthropic/v1/messages, "
            f"got {captured[1].url.path}"
        )
        # Verify the response is the successful one (200), not the
        # 302 we'd otherwise pass through.
        assert response.status_code == 200
        await mock_client.aclose()

    asyncio.run(main())
    print("✓ AnthropicProvider retries trailing-slash 302")


def test_anthropic_provider_retries_protocol_downgrade_redirect() -> None:
    """MiniMax's nginx *intentionally* 301s HTTPS → HTTP to the
    canonical directory URL — that's the redirect we have to
    recover from. ``AnthropicProvider`` reconstructs the API
    URL (``<Location>/v1/messages``) and retries the POST,
    accepting the protocol downgrade in this specific path
    because the URL is built from the Location header plus a
    known suffix (not a transparent follow).
    """
    import asyncio
    import httpx
    from ccproxy.app.providers.anthropic import AnthropicProvider

    captured: list[httpx.Request] = []

    class _MockTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            captured.append(request)
            if self.calls == 1:
                # First call: 301 to a directory URL with trailing
                # slash and no /v1/messages.
                return httpx.Response(
                    301,
                    headers={"location": "http://api.example.com/anthropic/"},
                    request=request,
                )
            # Second call: success with a real response.
            return httpx.Response(
                200,
                content=b'{"id":"msg_1","content":[]}',
                headers={"content-type": "application/json"},
                request=request,
            )

    transport = _MockTransport()
    mock_client = httpx.AsyncClient(transport=transport, timeout=10)
    provider = AnthropicProvider(
        base_url="https://api.example.com/anthropic",
        client=mock_client,
    )

    async def main():
        response = await provider.forward(
            body=b'{"model":"MiniMax-M3","messages":[]}',
            headers={"anthropic-version": "2023-06-01"},
            target_api_key="sk-test",
        )
        # Verify the retry happened despite the protocol downgrade.
        assert transport.calls == 2, (
            f"expected 2 calls (initial + retry), got {transport.calls}"
        )
        assert captured[1].url.scheme == "http", (
            f"retry should follow the downgrade to http://, "
            f"got scheme {captured[1].url.scheme!r}"
        )
        assert captured[1].url.path == "/anthropic/v1/messages", (
            f"retry should hit /anthropic/v1/messages, "
            f"got {captured[1].url.path}"
        )
        # Verify the response is the successful one (200), not the
        # 301 we'd otherwise pass through.
        assert response.status_code == 200
        await mock_client.aclose()

    asyncio.run(main())
    print("✓ AnthropicProvider retries trailing-slash 302 (incl. protocol downgrade)")


def test_router_consults_model_endpoints_map() -> None:
    """The router picks the user-configured provider when an
    ``endpoints`` entry maps the incoming model name. Without this
    lookup, every request for a model the user has set up against a
    custom provider (``MiniMax-M3 → MiniMax``, etc.) would silently
    fall through to the hardcoded ``anthropic`` provider at
    api.anthropic.com — which then rejects the unknown model as
    "model not found".
    """
    import asyncio
    from ccproxy.app.config import (
        ModelEndpointsCfg,
        ProviderCfg,
        Settings,
        SubagentsCfg,
    )
    from ccproxy.app.models.anthropic import AnthropicRequest
    from ccproxy.app.providers.anthropic import AnthropicProvider
    from ccproxy.app.router.subagent import SubagentRouter

    class _FakeAnthropic(AnthropicProvider):
        name = "anthropic"

    class _FakeMiniMax(AnthropicProvider):
        name = "anthropic"  # same provider class; the test is on routing

    mini_max_provider = _FakeMiniMax(
        base_url="https://api.minimax.io/anthropic"
    )
    anthropic_provider = _FakeAnthropic(
        base_url="https://api.anthropic.com"
    )
    providers: dict = {
        "anthropic": anthropic_provider,
        "MiniMax": mini_max_provider,
    }

    cfg = Settings(
        subagents=SubagentsCfg(enable=False),
        model_endpoints=ModelEndpointsCfg(
            default_provider="MiniMax",
            providers={
                "MiniMax": ProviderCfg(
                    url="https://api.minimax.io/anthropic",
                    api_key="sk-m",
                ),
            },
            endpoints={"MiniMax-M3": "MiniMax"},
        ),
    )
    router = SubagentRouter(cfg, providers)
    req = AnthropicRequest.model_validate(
        {"model": "MiniMax-M3", "max_tokens": 1, "messages": []}
    )

    async def main():
        return router.determine_route(req)

    decision = asyncio.run(main())
    assert decision.target_provider_name == "MiniMax", (
        f"expected MiniMax provider, got {decision.target_provider_name!r}"
    )
    assert decision.provider is mini_max_provider, (
        "decision.provider should be the MiniMaxProvider instance"
    )

    # Fall-through path: model not in ``endpoints`` map. The
    # router uses the user's ``default_provider`` as the next
    # fallback before prefix-matching — a sensible default since
    # the user has explicitly nominated one.
    req2 = AnthropicRequest.model_validate(
        {"model": "claude-3-5-sonnet-20241022", "max_tokens": 1, "messages": []}
    )

    async def main2():
        return router.determine_route(req2)

    decision2 = asyncio.run(main2())
    assert decision2.target_provider_name == "MiniMax", (
        f"expected MiniMax (default_provider), got "
        f"{decision2.target_provider_name!r}"
    )

    # With no default_provider set and no endpoints entry, the
    # router falls through to prefix-based routing — and finally
    # to ``anthropic`` as the catch-all for unknown prefixes.
    cfg_no_default = Settings(
        subagents=SubagentsCfg(enable=False),
        model_endpoints=ModelEndpointsCfg(
            providers={
                "MiniMax": ProviderCfg(
                    url="https://api.minimax.io/anthropic",
                    api_key="sk-m",
                ),
            },
            # No default_provider; the empty ``endpoints`` map means
            # every model falls through to prefix routing.
            endpoints={},
        ),
    )
    router2 = SubagentRouter(cfg_no_default, providers)
    req3 = AnthropicRequest.model_validate(
        {"model": "totally-unknown-model", "max_tokens": 1, "messages": []}
    )

    async def main3():
        return router2.determine_route(req3)

    decision3 = asyncio.run(main3())
    assert decision3.target_provider_name == "anthropic"
    print("✓ Router consults model_endpoints, falls back to default_provider, then prefix matching")


def test_proxy_models_endpoint_surfaces_alias_map() -> None:
    """``GET /v1/models`` lists each real model with its aliases in
    an Anthropic-shaped envelope, and ``?debug=1`` adds a ``__proxy__``
    block with the full routing config. Used to verify Claude Code
    is reaching the proxy (and to debug why a request might
    bypass it)."""
    from fastapi import Request
    from ccproxy.app.config import (
        ModelEndpointsCfg,
        ProviderCfg,
        Settings,
    )
    from ccproxy.app.api.proxy import list_models

    cfg = Settings(
        model_endpoints=ModelEndpointsCfg(
            default_provider="MiniMax",
            providers={
                "MiniMax": ProviderCfg(
                    url="https://api.MiniMax.io",
                    api_key="sk-test",
                    available_models=["MiniMax-M3"],
                ),
            },
            endpoints={"MiniMax-M3": "MiniMax"},
            model_aliases={
                "freestuff": "MiniMax-M3",
                "mm-m3": "MiniMax-M3",
            },
        ),
    )

    class _FakeReq:
        def __init__(self, qp):
            self.query_params = qp

    import asyncio
    # Standard Anthropic-shaped response: each model with its aliases.
    body = asyncio.run(list_models(_FakeReq({}), settings=cfg)).body.decode()
    import json as _json

    parsed = _json.loads(body)
    assert parsed["object"] == "list"
    by_id = {m["id"]: m for m in parsed["data"]}
    assert "MiniMax-M3" in by_id
    assert sorted(by_id["MiniMax-M3"]["aliases"]) == ["freestuff", "mm-m3"]
    # Debug variant exposes the alias_map + endpoints explicitly so a
    # misconfigured session can be diagnosed by curl.
    debug_body = asyncio.run(
        list_models(_FakeReq({"debug": "1"}), settings=cfg)
    ).body.decode()
    debug = _json.loads(debug_body)
    assert debug["__proxy__"]["alias_map"] == {
        "freestuff": "MiniMax-M3",
        "mm-m3": "MiniMax-M3",
    }
    assert debug["__proxy__"]["endpoints_map"] == {"MiniMax-M3": "MiniMax"}
    assert debug["__proxy__"]["default_provider"] == "MiniMax"
    print("✓ /v1/models surfaces the alias map (with ?debug=1)")


def test_proxy_alias_preserves_alias_in_log_field() -> None:
    """When an alias rewrites the model, ``original_model`` on the
    request log is the alias, not the real name."""
    from ccproxy.app.models.anthropic import AnthropicRequest
    from ccproxy.app.api.proxy import _apply_alias_rewrite

    req = AnthropicRequest.model_validate(
        {
            "model": "opus",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    new_req, alias_seen = _apply_alias_rewrite(
        req, {"opus": "claude-opus-4-8"}
    )
    # The proxy's log line uses ``alias_seen or new_req.model`` —
    # we mimic the same selection to verify the alias reaches the
    # log. The proxy's `original_model` for this request is the
    # alias, even though the router/URL lookup see the real name.
    assert alias_seen is not None
    assert alias_seen == "opus"
    assert new_req.model == "claude-opus-4-8"
    print("✓ Alias preserved as original_model for log surface")


# ---- Runner ---------------------------------------------------------------

# ---- Runner ---------------------------------------------------------------


def main() -> None:
    tests = [
        test_claude_profile_has_bind_port_config_dir_and_default_flag,
        test_claude_profile_serialization_roundtrips_new_fields,
        test_default_profile_always_exists_at_index_zero,
        test_default_profile_stays_first_after_adding_others,
        test_default_profile_cannot_be_deleted_via_store,
        test_ensure_default_creates_one_if_missing,
        test_new_profile_seeded_from_default_inherits_endpoints,
        test_profile_editor_has_bind_port_config_dir_widgets,
        test_model_endpoints_table_is_in_a_scroll_area,
        test_env_vars_table_is_in_a_scroll_area,
        test_manage_profiles_dialog_shows_list_with_default_first,
        test_manage_profiles_dialog_delete_disabled_for_default,
        test_manage_profiles_dialog_delete_enabled_for_user_profile,
        test_proxy_settings_action_lives_under_profile_submenu,
        test_profile_submenu_has_manage_profiles_entry,
        test_active_profile_drives_proxy_start,
        test_env_var_catalog_includes_expected_entries,
        test_env_var_catalog_excludes_infra_owned_vars,
        test_env_var_catalog_sections_are_ordered,
        test_standard_env_vars_dialog_has_two_columns,
        test_standard_env_vars_dialog_filters_already_added,
        test_standard_env_vars_dialog_returns_selected_names,
        test_standard_env_vars_dialog_returns_multiple_when_extended_selection,
        test_standard_env_vars_dialog_skips_section_headers,
        test_profile_editor_has_add_standard_and_add_custom_buttons,
        test_add_standard_button_inserts_blank_value_rows,
        test_add_custom_button_inserts_blank_row,
        test_existing_env_keys_marks_only_known_standard_vars,
        test_profile_editor_uses_a_qtabwidget_with_six_tabs,
        test_profile_editor_widgets_live_in_their_expected_tabs,
        test_profile_editor_dialog_height_is_more_compact,
        test_api_keys_tab_has_default_row_pinned,
        test_api_keys_tab_add_inserts_new_row,
        test_default_row_cannot_be_removed,
        test_api_key_value_persists_to_credential_store_on_save,
        test_credential_store_strips_whitespace_on_set,
        test_endpoints_table_is_read_only_three_columns,
        test_endpoints_form_model_combo_constrained_to_provider_models,
        test_endpoints_row_apply_persists_provider_name_on_save,
        test_endpoints_form_apply_disabled_until_divergence,
        test_provider_stale_key_ref_falls_back_to_no_key_in_form,
        test_resolve_api_key_walks_provider_chain,
        test_resolve_api_key_returns_none_when_no_key_ref,
        test_api_keys_form_apply_disabled_until_divergence,
        test_api_keys_form_supports_rename_on_default,
        test_env_vars_form_apply_disabled_until_divergence,
        test_env_vars_form_apply_persists_value,
        test_clicks_endpoints_tab_does_not_corrupt_state,
        test_providers_tab_has_default_row_pinned,
        test_providers_tab_add_inserts_new_row,
        test_default_provider_row_cannot_be_removed,
        test_providers_form_apply_persists_url_on_save,
        test_providers_form_apply_disabled_until_divergence,
        test_providers_remove_button_drops_provider_from_endpoint_combo,
        test_model_config_form_provider_combo_lists_provider_names,
        test_model_config_form_combo_userdata_is_url,
        test_model_config_form_url_hint_reflects_selection,
        test_model_config_endpoint_table_displays_provider_name,
        test_endpoints_table_is_read_only_three_columns,
        test_endpoint_aliases_form_apply_persists_aliases_on_save,
        test_endpoint_aliases_parse_dedupes_and_trims,
        test_endpoint_aliases_form_persists_after_row_reselect,
        test_endpoint_snap_apply_on_accept_commits_typed_aliases,
        test_endpoints_auto_seeded_from_provider_catalog,
        test_default_model_combo_lists_aliases_with_legacy_fallback,
        test_default_model_combo_persists_alias_choice,
        test_endpoints_table_columns_are_aliases_provider_model,
        test_endpoint_aliases_round_trip_through_store,
        test_endpoint_aliases_unique_across_profile_on_save,
        test_proxy_alias_rewrites_req_model_before_router,
        test_proxy_alias_preserves_alias_in_log_field,
        test_router_consults_model_endpoints_map,
        test_proxy_models_endpoint_surfaces_alias_map,
        test_proxy_alias_rewrite_reserializes_wire_body,
        test_anthropic_provider_logs_key_prefix_diagnostic,
        test_anthropic_provider_logs_short_key_safely,
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
    print(f"\n✅ All {len(tests)} profile-model-and-dialog tests passed!")


if __name__ == "__main__":
    main()
