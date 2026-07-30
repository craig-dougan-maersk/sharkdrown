"""Tests for the world-clock feature.

Covers:

- ``Clock`` / ``ClockCollection`` round-trip through ``QSettings``
- Default state when nothing has been written
- ``ClocksDialog`` add / edit / remove flows
- ``ClockToolbarWidget`` show / hide behaviour
- ``ClockToolbarWidget`` 12h / 24h label formatting
- Master toggle persists alongside the per-clock list
- Timezone validation in the editor dialog
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# Force offscreen rendering — the widget rebuilds labels on each
# tick and we don't need a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _clear_qsettings(qapp, isolated_settings):
    """Clear the per-test QSettings store before each test.

    The shared ``isolated_settings`` fixture redirects the INI path
    to a tmp dir but doesn't clear it. Tests that depend on a fresh
    state (e.g. ``test_load_clocks_returns_disabled_empty_when_no_settings``)
    would otherwise see data left over from prior smoke runs or
    earlier tests in the same file.
    """
    from PySide6.QtCore import QSettings

    QSettings("SharkDrown", "App").clear()
    yield


# ---- persistence ----------------------------------------------------------


def test_load_clocks_returns_disabled_empty_when_no_settings(
    qapp, isolated_settings
) -> None:
    """Fresh ``QSettings`` store returns a disabled, empty collection.

    A fresh install shouldn't display clocks out of the box.
    """
    from src.ui.clocks import load_clocks

    coll = load_clocks()
    assert coll.enabled is False
    assert coll.clocks == []


def test_clock_round_trips_through_qsettings(qapp, isolated_settings) -> None:
    """Save then load returns an equal collection.

    Catches the easy bug of adding a new field to ``Clock`` but
    forgetting to (de)serialise it.
    """
    from src.ui.clocks import (
        Clock,
        ClockCollection,
        load_clocks,
        save_clocks,
    )

    coll = ClockCollection(
        enabled=True,
        clocks=[
            Clock(id=1, timezone="America/New_York", use_24h=False),
            Clock(id=2, timezone="Europe/London", use_24h=True),
            Clock(id=3, timezone="Asia/Tokyo", use_24h=True),
        ],
    )
    save_clocks(coll)
    loaded = load_clocks()
    assert loaded.enabled == coll.enabled
    assert loaded.clocks == coll.clocks


def test_save_drops_entries_with_empty_timezone(
    qapp, isolated_settings
) -> None:
    """An entry whose timezone is empty (the user cleared the
    combo) doesn't survive a save+reload — silently dropped so we
    don't render ``—:—:—`` for a half-built row."""
    from src.ui.clocks import (
        Clock,
        ClockCollection,
        load_clocks,
        save_clocks,
    )

    coll = ClockCollection(
        enabled=True,
        clocks=[
            Clock(id=1, timezone="America/New_York", use_24h=True),
            Clock(id=2, timezone="", use_24h=True),
        ],
    )
    save_clocks(coll)
    loaded = load_clocks()
    assert len(loaded.clocks) == 1
    assert loaded.clocks[0].id == 1


def test_next_clock_id_returns_one_above_max(
    qapp, isolated_settings
) -> None:
    """``next_clock_id`` returns one above the highest existing id.

    Used by the dialog so a freshly-added clock doesn't collide
    with existing ids.
    """
    from src.ui.clocks import Clock, next_clock_id

    existing = [
        Clock(id=3, timezone="UTC", use_24h=True),
        Clock(id=7, timezone="UTC", use_24h=True),
        Clock(id=5, timezone="UTC", use_24h=True),
    ]
    assert next_clock_id(existing) == 8
    assert next_clock_id([]) == 1


# ---- QSettings namespace --------------------------------------------------


def test_qsettings_keys_are_top_level_under_sharkdrown_app(
    qapp, isolated_settings, tmp_path
) -> None:
    """``save_clocks`` writes the two expected keys at the top level
    of ``QSettings("SharkDrown", "App")``.

    Catches the easy mistake of writing under a group prefix that
    would silently move them out of ``load_clocks``'s read path.
    """
    from PySide6.QtCore import QSettings

    from src.ui.clocks import (
        Clock,
        ClockCollection,
        save_clocks,
    )

    save_clocks(
        ClockCollection(
            enabled=True,
            clocks=[Clock(id=1, timezone="UTC", use_24h=True)],
        )
    )
    s = QSettings("SharkDrown", "App")
    assert s.value("clocks_enabled", None, type=bool) is True
    raw = s.value("clocks_data", None, type=str)
    assert raw is not None
    parsed = json.loads(raw)
    assert parsed == [{"id": 1, "timezone": "UTC", "use_24h": True}]


# ---- ClocksDialog ---------------------------------------------------------


def test_clocks_dialog_populates_table_from_settings(
    qapp, isolated_settings
) -> None:
    """Opening the dialog against a saved collection fills the table
    rows with the matching clocks."""
    from src.ui.clocks import (
        Clock,
        ClockCollection,
        ClocksDialog,
        save_clocks,
    )

    save_clocks(
        ClockCollection(
            enabled=True,
            clocks=[
                Clock(id=1, timezone="America/New_York", use_24h=False),
                Clock(id=2, timezone="Europe/London", use_24h=True),
            ],
        )
    )
    dlg = ClocksDialog()
    try:
        assert dlg.enabled_checkbox.isChecked() is True
        assert dlg.table.rowCount() == 2
        assert dlg.table.item(0, 0).text() == "1"
        assert dlg.table.item(0, 1).text() == "America/New_York"
        assert dlg.table.item(0, 2).text() == "12h"
        assert dlg.table.item(1, 2).text() == "24h"
    finally:
        dlg.deleteLater()


def test_clocks_dialog_add_appends_a_row(
    qapp, isolated_settings, monkeypatch
) -> None:
    """``_on_add`` opens the editor (we patch ``exec`` to return
    Accepted with a known clock) and the row appears in the table."""
    from PySide6.QtWidgets import QDialog

    from src.ui.clocks import (
        Clock,
        ClockEditorDialog,
        ClocksDialog,
    )

    new_clock = Clock(id=42, timezone="Asia/Tokyo", use_24h=True)

    def fake_to_clock(self, *, base_id):
        return new_clock

    monkeypatch.setattr(ClockEditorDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ClockEditorDialog, "to_clock", fake_to_clock)

    dlg = ClocksDialog()
    try:
        assert dlg.table.rowCount() == 0
        dlg._on_add()
        assert dlg.table.rowCount() == 1
        assert dlg.table.item(0, 1).text() == "Asia/Tokyo"
        assert dlg.table.item(0, 2).text() == "24h"
    finally:
        dlg.deleteLater()


def test_clocks_dialog_remove_drops_the_row(
    qapp, isolated_settings, monkeypatch
) -> None:
    """``_on_remove`` strips the selected clock from the in-memory
    collection and the table."""
    from src.ui.clocks import (
        Clock,
        ClockCollection,
        ClocksDialog,
        save_clocks,
    )

    save_clocks(
        ClockCollection(
            enabled=True,
            clocks=[
                Clock(id=1, timezone="UTC", use_24h=True),
                Clock(id=2, timezone="UTC", use_24h=True),
            ],
        )
    )
    dlg = ClocksDialog()
    try:
        assert dlg.table.rowCount() == 2
        dlg.table.selectRow(0)
        dlg._on_remove()
        assert dlg.table.rowCount() == 1
        assert dlg._collection.clocks[0].id == 2
    finally:
        dlg.deleteLater()


def test_clocks_dialog_accept_persists_changes(
    qapp, isolated_settings
) -> None:
    """OK on the dialog writes the collection to ``QSettings``."""
    from src.ui.clocks import (
        Clock,
        ClockCollection,
        ClocksDialog,
        load_clocks,
        save_clocks,
    )

    save_clocks(ClockCollection(enabled=False, clocks=[]))
    dlg = ClocksDialog()
    try:
        dlg.enabled_checkbox.setChecked(True)
        dlg._collection.clocks.append(
            Clock(id=99, timezone="Pacific/Auckland", use_24h=True)
        )
        dlg._on_accept()
        loaded = load_clocks()
        assert loaded.enabled is True
        assert len(loaded.clocks) == 1
        assert loaded.clocks[0].timezone == "Pacific/Auckland"
    finally:
        dlg.deleteLater()


def test_clocks_dialog_rejects_unknown_timezone(
    qapp, isolated_settings, monkeypatch
) -> None:
    """Typing a non-IANA string into the timezone combo and clicking
    OK surfaces a warning rather than persisting a bad value."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    from src.ui.clocks import ClockEditorDialog

    warned = {"called": False}

    def fake_warning(*a, **kw):
        warned["called"] = True
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", fake_warning)

    dlg = ClockEditorDialog()
    try:
        dlg.timezone_combo.setEditText("Not/A_Real_Zone")
        dlg._on_accept()
        assert warned["called"] is True, (
            "Unknown-timezone OK should have raised a warning"
        )
    finally:
        dlg.deleteLater()


# ---- ClockToolbarWidget ---------------------------------------------------


def _coll(enabled: bool, *clocks: Clock):
    """Helper to build a collection with a list of clocks inline."""
    from src.ui.clocks import ClockCollection

    return ClockCollection(enabled=enabled, clocks=list(clocks))


def test_toolbar_hides_when_disabled(qapp) -> None:
    """When ``enabled=False`` the toolbar is hidden regardless of
    how many clocks are configured."""
    from src.ui.clocks import Clock, ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w.refresh()  # ensure initial empty state
        coll = _coll(
            False,
            Clock(id=1, timezone="UTC", use_24h=True),
        )
        # ``_collection`` is internal — poke directly to avoid
        # writing to QSettings from a test.
        w._collection = coll
        w._rebuild_labels()
        assert w.isVisible() is False
    finally:
        w.deleteLater()


def test_toolbar_hides_when_no_clocks(qapp) -> None:
    """Empty clocks list also hides the widget, even when enabled."""
    from src.ui.clocks import ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w._collection = _coll(True)  # enabled but empty
        w._rebuild_labels()
        assert w.isVisible() is False
    finally:
        w.deleteLater()


def test_toolbar_shows_one_label_per_clock(qapp) -> None:
    """Three clocks → three labels (and two dividers between them)."""
    from src.ui.clocks import Clock, ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w._collection = _coll(
            True,
            Clock(id=1, timezone="UTC", use_24h=True),
            Clock(id=2, timezone="UTC", use_24h=True),
            Clock(id=3, timezone="UTC", use_24h=True),
        )
        w._rebuild_labels()
        assert w.isVisible() is True
        assert len(w._clock_labels) == 3
        # Two dividers between three clocks (none before the first
        # or after the last).
        assert sum(1 for d in w._divider_labels if d is not None) == 2
    finally:
        w.deleteLater()


def test_toolbar_tick_formats_24h_clock(qapp) -> None:
    """A 24-hour clock label contains a 2-digit hour and
    ``:`` separators — and no AM/PM token."""
    from src.ui.clocks import Clock, ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w._collection = _coll(
            True, Clock(id=1, timezone="UTC", use_24h=True)
        )
        w._rebuild_labels()
        w._tick()
        text = w._clock_labels[0].text()
        assert ":" in text
        assert "AM" not in text and "PM" not in text
    finally:
        w.deleteLater()


def test_toolbar_tick_formats_12h_clock(qapp) -> None:
    """A 12-hour clock label includes AM or PM (Python's ``%p``
    uppercases on Linux; Windows uses different conventions — accept
    either case)."""
    from src.ui.clocks import Clock, ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w._collection = _coll(
            True, Clock(id=1, timezone="UTC", use_24h=False)
        )
        w._rebuild_labels()
        w._tick()
        text = w._clock_labels[0].text().upper()
        assert ":" in text
        assert "AM" in text or "PM" in text
    finally:
        w.deleteLater()


def test_toolbar_tick_survives_unknown_timezone(qapp) -> None:
    """A bogus timezone (the user manually edited the INI file)
    surfaces ``—:—:—`` in the label rather than crashing the tick."""
    from src.ui.clocks import Clock, ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w._collection = _coll(
            True, Clock(id=1, timezone="Not/A_Real_Zone", use_24h=True)
        )
        w._rebuild_labels()
        w._tick()  # must not raise
        assert "—:—:—" in w._clock_labels[0].text()
    finally:
        w.deleteLater()


def test_toolbar_stop_timer_is_idempotent(qapp) -> None:
    """``stop_timer`` can be called multiple times safely."""
    from src.ui.clocks import ClockToolbarWidget

    w = ClockToolbarWidget()
    try:
        w.stop_timer()
        w.stop_timer()  # second call: still fine, no exception
    finally:
        w.deleteLater()


# ---- View menu wiring ----------------------------------------------------


def test_view_menu_has_clocks_action(qapp) -> None:
    """``MainWindow`` exposes a ``&Clocks…`` action under the View
    menu. Catches the easy mistake of forgetting to add the action
    to the menu after refactoring.
    """
    from PySide6.QtGui import QAction

    from src.ui.main_window.window import MainWindow

    w = MainWindow()
    try:
        menubar = w.menuBar()
        view_menu = None
        for action in menubar.actions():
            text = action.text().replace("&", "")
            if text == "View":
                view_menu = action.menu()
                break
        assert view_menu is not None, "View menu not found"
        labels = [
            a.text().replace("&", "")
            for a in view_menu.actions()
            if isinstance(a, QAction)
        ]
        assert "Clocks…" in labels, (
            f"Expected 'Clocks…' in View menu; got {labels}"
        )
    finally:
        w.close()