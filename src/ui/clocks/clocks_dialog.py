"""Manager dialog for the world-clock feature.

A single-tab :class:`QDialog` with:

- A master ``QCheckBox`` for the global enable toggle.
- A ``QTableWidget`` listing every configured clock (id, timezone,
  format).
- Add / Edit / Remove buttons (Add and Edit both open
  :class:`_ClockEditorDialog`).

On OK, the dialog writes through to :func:`src.ui.clocks.models.save_clocks`
so a follow-up call to :meth:`ClockToolbarWidget.refresh` picks the
changes up.
"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.clocks.models import (
    Clock,
    ClockCollection,
    load_clocks,
    next_clock_id,
    save_clocks,
)


class ClockEditorDialog(QDialog):
    """Small editor for one :class:`Clock`.

    Two fields:
      - ``Timezone`` — ``QComboBox`` populated from
        :func:`zoneinfo.available_timezones`, sorted, with the local
        timezone first (preselected). Editable so the user can type a
        zone that's not in the dropdown — ``QComboBox`` keeps an
        "other" entry when the text doesn't match.
      - ``Format`` — ``QComboBox`` with ``"12-hour (AM/PM)"`` /
        ``"24-hour"``.

    Used for both Add and Edit; the caller passes the starting
    :class:`Clock` (or ``None`` for Add).
    """

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clock" if clock is None else f"Clock — {clock.timezone}")
        self.setModal(True)
        self.resize(420, 200)
        self._initial = clock

        layout = QFormLayout(self)

        # ---- Timezone --------------------------------------------------
        self.timezone_combo = QComboBox()
        self.timezone_combo.setEditable(True)
        # ``InsertPolicy.NoInsert`` so the typed-in text isn't
        # auto-added to the dropdown on OK — we only accept what's
        # either selected or explicitly typed.
        self.timezone_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        zones = _sorted_timezones()
        self.timezone_combo.addItems(zones)
        if clock is not None and clock.timezone:
            # ``setCurrentText`` handles both exact-match and
            # no-match (no-op for no-match, which is fine — the
            # user typed it manually and we'll preserve their text).
            idx = self.timezone_combo.findText(clock.timezone)
            if idx >= 0:
                self.timezone_combo.setCurrentIndex(idx)
            else:
                # Add the value without inserting into the dropdown
                # permanently.
                self.timezone_combo.setEditText(clock.timezone)
        else:
            # New entry — preselect the local timezone so the user
            # only has to confirm.
            local = _local_timezone_name()
            idx = self.timezone_combo.findText(local)
            if idx >= 0:
                self.timezone_combo.setCurrentIndex(idx)
            else:
                self.timezone_combo.setEditText(local)
        layout.addRow("Timezone:", self.timezone_combo)

        # ---- Format ----------------------------------------------------
        self.format_combo = QComboBox()
        self.format_combo.addItems(["12-hour (AM/PM)", "24-hour"])
        self.format_combo.setCurrentIndex(0 if (clock is None or not clock.use_24h) else 1)
        layout.addRow("Format:", self.format_combo)

        # ---- Buttons ---------------------------------------------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_accept(self) -> None:
        tz = self.timezone_combo.currentText().strip()
        if not tz:
            QMessageBox.warning(self, "Missing timezone", "Pick or type a timezone.")
            return
        # Validate against ``ZoneInfo`` so a typo (e.g. ``"Local"``)
        # is caught here rather than producing ``—:—:—`` in the
        # toolbar at runtime.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            QMessageBox.warning(
                self,
                "Unknown timezone",
                f"{tz!r} isn't a known IANA timezone. "
                "Pick one from the dropdown or use a value like "
                "'America/New_York' / 'Europe/London' / 'UTC'.",
            )
            return
        self.accept()

    def to_clock(self, *, base_id: int) -> Clock:
        """Return the edited clock. ``base_id`` is what the dialog
        passes back to ``ClockCollection`` so the caller can supply
        a fresh id for an Add."""
        tz = self.timezone_combo.currentText().strip()
        use_24h = self.format_combo.currentIndex() == 1
        # If we were given an existing clock, keep its id; the
        # ``base_id`` is ignored in that path.
        cid = self._initial.id if self._initial is not None else base_id
        return Clock(id=cid, timezone=tz, use_24h=use_24h)


def _sorted_timezones() -> list[str]:
    """Return the IANA tz list sorted with the local zone first."""
    from datetime import datetime
    from zoneinfo import ZoneInfo, available_timezones

    try:
        local_name = datetime.now().astimezone().tzinfo.key  # type: ignore[union-attr]
    except (AttributeError, KeyError):
        local_name = "UTC"
    zones = sorted(available_timezones())
    # ``available_timezones`` is a frozenset — sorting it is fine,
    # but we want the local zone at the top for the user.
    if local_name in zones:
        zones.remove(local_name)
        zones.insert(0, local_name)
    return zones


def _local_timezone_name() -> str:
    """Best-effort local IANA timezone name, falling back to UTC."""
    from datetime import datetime

    try:
        return datetime.now().astimezone().tzinfo.key  # type: ignore[union-attr]
    except (AttributeError, KeyError):
        return "UTC"


class ClocksDialog(QDialog):
    """Manager dialog for the user's world clocks.

    Mirrors :class:`src.ui.terminal.settings_dialog.TerminalSettingsDialog`'s
    shape: ``QVBoxLayout``, help text at the top, content in the
    middle, OK / Cancel at the bottom. Static
    :meth:`open_dialog` returns ``True`` when the user accepted and
    the collection was persisted.
    """

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("World Clocks")
        self.setModal(True)
        self.resize(560, 360)

        # Work on a deep copy so Cancel discards changes without us
        # having to round-trip QSettings twice.
        self._collection: ClockCollection = copy.deepcopy(load_clocks())
        # Pending clocks the user added but hasn't yet committed
        # via Apply / OK. Stored as a parallel list so a failed Add
        # (e.g. user clicks Edit then Cancel on the editor) doesn't
        # leak a half-built row into the table.
        self._build_ui()
        self._refresh_table()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        help_label = QLabel(
            "Show one or more clocks in the bottom-left of the status "
            "bar. Each clock ticks every second using your system's "
            "timezone database. Enable the master toggle below to "
            "show them; turn it off to hide the entire row."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(help_label)

        # Master toggle — bound directly to the collection so the
        # OK handler doesn't have to re-read it from the widget.
        from PySide6.QtWidgets import QCheckBox

        self.enabled_checkbox = QCheckBox("Show world clocks in status bar")
        self.enabled_checkbox.setChecked(self._collection.enabled)
        layout.addWidget(self.enabled_checkbox)

        # ---- Table ---------------------------------------------------
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["ID", "Timezone", "Format"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(140)
        layout.addWidget(self.table, 1)

        # ---- Buttons -------------------------------------------------
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("Add…")
        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn = QPushButton("Edit…")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._on_edit)
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.setEnabled(False)
        self.remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.remove_btn)
        btn_row.addStretch(1)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        layout.addWidget(btn_container)

        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        # ---- Dialog buttons ------------------------------------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---- population ---------------------------------------------------

    def _refresh_table(self) -> None:
        """Sync the table to ``self._collection.clocks``.

        Called after Add / Edit / Remove. Stores the source :class:`Clock`
        on the row's ID cell under ``Qt.ItemDataRole.UserRole` so the
        edit / remove handlers don't have to re-look it up by id.
        """
        self.table.setRowCount(0)
        for clock in self._collection.clocks:
            self._append_row(clock)

    def _append_row(self, clock: Clock) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        # ID column — read-only; carries the source Clock instance in
        # UserRole so the handlers can mutate without re-lookup.
        id_item = QTableWidgetItem(str(clock.id))
        id_item.setFlags(id_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        id_item.setData(Qt.ItemDataRole.UserRole, clock)
        self.table.setItem(row, 0, id_item)
        # Timezone column
        tz_item = QTableWidgetItem(clock.timezone)
        tz_item.setFlags(tz_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 1, tz_item)
        # Format column
        fmt_item = QTableWidgetItem("24h" if clock.use_24h else "12h")
        fmt_item.setFlags(fmt_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, 2, fmt_item)

    # ---- selection-driven UI state ------------------------------------

    def _on_selection_changed(self) -> None:
        has_sel = self.table.currentRow() >= 0
        self.edit_btn.setEnabled(has_sel)
        self.remove_btn.setEnabled(has_sel)

    # ---- add / edit / remove slots ------------------------------------

    def _on_add(self) -> None:
        dlg = ClockEditorDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_clock = dlg.to_clock(base_id=next_clock_id(self._collection.clocks))
        self._collection.clocks.append(new_clock)
        self._refresh_table()
        # Auto-select the new row so the user can immediately edit
        # / remove if they got something wrong.
        new_row = self.table.rowCount() - 1
        if new_row >= 0:
            self.table.selectRow(new_row)

    def _on_edit(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        existing = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if not isinstance(existing, Clock):
            return
        dlg = ClockEditorDialog(clock=existing, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.to_clock(base_id=existing.id)
        # Replace in place — preserves id and position.
        for i, c in enumerate(self._collection.clocks):
            if c.id == existing.id:
                self._collection.clocks[i] = updated
                break
        self._refresh_table()
        self.table.selectRow(row)

    def _on_remove(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        existing = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if not isinstance(existing, Clock):
            return
        self._collection.clocks = [
            c for c in self._collection.clocks if c.id != existing.id
        ]
        self._refresh_table()
        # Selection collapses — ``_on_selection_changed`` will
        # disable the buttons again.

    # ---- accept ------------------------------------------------------

    def _on_accept(self) -> None:
        # Pull the master toggle and persist the whole collection.
        self._collection.enabled = self.enabled_checkbox.isChecked()
        save_clocks(self._collection)
        self.accept()

    # ---- static helper -----------------------------------------------

    @staticmethod
    def open_dialog(parent: QWidget | None = None) -> bool:
        """Show the dialog modally; return ``True`` if accepted."""
        dlg = ClocksDialog(parent=parent)
        return dlg.exec() == QDialog.DialogCode.Accepted