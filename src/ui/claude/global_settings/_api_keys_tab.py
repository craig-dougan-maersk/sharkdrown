"""API Keys tab mixin for the global settings dialog.

Ported from ``profile_editor/_api_keys_tab.py``. In the global
context, the credential store is the user's single source of truth
— there's no per-profile credential scope. Apply / Remove mutate
the on-disk store directly via the dialog's
:meth:`_current_credential_store` accessor.

The select-and-edit pattern is otherwise identical: read-only
``QTableWidget`` with the form below mirroring the selected row's
*name* + *value* (the value column is masked; the form's value
field is blank by default so the user typing replaces the
existing value or leaving blank keeps it).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.credential_store import CredentialStore


class ApiKeysTabMixin:
    def _build_api_key_tab(self) -> None:
        """Tab 3: global credential store editor."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        cred_help = QLabel(
            "Named API keys stored in <code>credentials.json</code> "
            "(chmod 0600). The <b>default</b> key is always present and "
            "cannot be removed. Select a row to edit its name and value, "
            "then click <b>Apply</b>. Other keys can be added with "
            "<b>Add…</b> and removed with <b>Remove Selected</b>."
        )
        cred_help.setWordWrap(True)
        cred_help.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(cred_help)

        self.keys_table = QTableWidget(0, 2)
        self.keys_table.setHorizontalHeaderLabels(["Name", "Value"])
        self.keys_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.keys_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.keys_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.keys_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.keys_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.keys_table.verticalHeader().setVisible(False)
        self.keys_table.itemSelectionChanged.connect(
            self._on_key_selection_changed
        )
        self.keys_table.setMinimumHeight(140)
        self.keys_table_scroll = self._wrap_in_scroll(
            self.keys_table, max_height=180
        )
        tab_layout.addWidget(self.keys_table_scroll)

        keys_btns = QHBoxLayout()
        keys_add = QPushButton("Add…")
        keys_add.clicked.connect(self._on_key_add)
        keys_remove = QPushButton("Remove Selected")
        keys_remove.clicked.connect(self._on_key_remove)
        self.keys_remove_btn = keys_remove
        keys_btns.addWidget(keys_add)
        keys_btns.addWidget(keys_remove)
        keys_btns.addStretch(1)
        keys_btn_container = QWidget()
        keys_btn_container.setLayout(keys_btns)
        tab_layout.addWidget(keys_btn_container)

        editor_box = QGroupBox("Edit selected key")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        self._selected_key_row: int = -1
        self._key_row_snapshot: tuple[str, str] | None = None

        self.key_name_edit = QLineEdit()
        self.key_name_edit.setPlaceholderText("e.g. work")
        editor_layout.addRow("Name:", self.key_name_edit)

        self.key_value_edit = QLineEdit()
        self.key_value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_value_edit.setPlaceholderText(
            "(leave blank to keep existing value)"
        )
        editor_layout.addRow("Key value:", self.key_value_edit)

        self.key_name_edit.textChanged.connect(self._on_key_form_changed)
        self.key_value_edit.textChanged.connect(self._on_key_form_changed)

        btn_row = QHBoxLayout()
        self.key_apply_btn = QPushButton("Apply")
        self.key_apply_btn.setEnabled(False)
        self.key_apply_btn.clicked.connect(self._on_key_apply)
        self.key_revert_btn = QPushButton("Revert")
        self.key_revert_btn.setEnabled(False)
        self.key_revert_btn.clicked.connect(self._on_key_revert)
        btn_row.addStretch(1)
        btn_row.addWidget(self.key_apply_btn)
        btn_row.addWidget(self.key_revert_btn)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        editor_layout.addRow("", btn_container)

        self.key_form_status = QLabel("Select a row to edit")
        self.key_form_status.setStyleSheet(
            "color: gray; font-size: 11px;"
        )
        editor_layout.addRow("", self.key_form_status)

        self._set_key_form_enabled(False)

        tab_layout.addWidget(editor_box)
        tab_layout.addStretch(1)

        self.tabs.addTab(tab, "API Keys")

    def _populate_keys_table(self) -> None:
        self.keys_table.setRowCount(0)
        try:
            all_refs = self._current_credential_store().all_refs()
        except (OSError, ValueError):
            all_refs = {}
        # Pinned default row.
        default_value = all_refs.get("default", "")
        self._append_key_row(
            "default",
            default_value,
            is_default=True,
        )
        for name, value in all_refs.items():
            if name == "default":
                continue
            self._append_key_row(name, value, is_default=False)

    def _append_key_row(self, name: str, value: str, *, is_default: bool) -> None:
        row = self.keys_table.rowCount()
        self.keys_table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, is_default)
        self.keys_table.setItem(row, 0, name_item)
        if value:
            value_text = "•" * min(len(value), 16)
        else:
            value_text = "(not set)"
        value_item = QTableWidgetItem(value_text)
        value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.keys_table.setItem(row, 1, value_item)

    def _current_key_names(self) -> list[str]:
        names: list[str] = []
        for row in range(self.keys_table.rowCount()):
            item = self.keys_table.item(row, 0)
            if item is None:
                continue
            name = item.text().strip()
            if name and name not in names:
                names.append(name)
        return names

    def _current_credential_store(self) -> CredentialStore:
        from src.profiles.credential_store import CredentialStore
        from src.proxy.paths import data_dir

        store = getattr(self, "_credential_store", None)
        if store is None:
            store = CredentialStore(data_dir() / "credentials.json")
            self._credential_store = store
        return store

    def _on_key_selection_changed(self) -> None:
        sel = self.keys_table.selectionModel()
        row = (
            sel.selectedRows()[0].row() if sel and sel.selectedRows() else -1
        )
        self._selected_key_row = row
        if row < 0:
            self._key_row_snapshot = None
            self._set_key_form_enabled(False)
            self.key_form_status.setText("Select a row to edit")
            self._refresh_key_apply_state()
            if hasattr(self, "keys_remove_btn"):
                self.keys_remove_btn.setEnabled(False)
            return
        name_item = self.keys_table.item(row, 0)
        value_item = self.keys_table.item(row, 1)
        name_text = name_item.text() if name_item is not None else ""
        value_text = value_item.text() if value_item is not None else ""
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole)
            if name_item is not None
            else False
        )
        self._set_key_form_enabled(True)
        self.key_name_edit.blockSignals(True)
        self.key_value_edit.blockSignals(True)
        try:
            self.key_name_edit.setText(name_text)
            self.key_value_edit.setText("")
        finally:
            self.key_name_edit.blockSignals(False)
            self.key_value_edit.blockSignals(False)
        self._key_row_snapshot = (name_text, value_text)
        self.key_form_status.setText(
            f"Editing row {row + 1} of {self.keys_table.rowCount()}"
        )
        if hasattr(self, "keys_remove_btn"):
            self.keys_remove_btn.setEnabled(row > 0 and not is_default)
        self._refresh_key_apply_state()

    def _set_key_form_enabled(self, enabled: bool) -> None:
        self.key_name_edit.setEnabled(enabled)
        self.key_value_edit.setEnabled(enabled)
        if not enabled:
            self.key_apply_btn.setEnabled(False)
            self.key_revert_btn.setEnabled(False)

    def _on_key_form_changed(self, *_args: object) -> None:
        if getattr(self, "_selected_key_row", -1) < 0:
            return
        self._refresh_key_apply_state()

    def _refresh_key_apply_state(self) -> None:
        enabled_divergence = self._key_form_values_diverge_from_row()
        self.key_apply_btn.setEnabled(enabled_divergence)
        self.key_revert_btn.setEnabled(enabled_divergence)
        if enabled_divergence:
            self.key_form_status.setText(
                "Edited — click Apply to save, or Revert to discard"
            )
        elif self._selected_key_row >= 0:
            self.key_form_status.setText(
                f"Row {self._selected_key_row + 1} — no pending changes"
            )

    def _key_form_values_diverge_from_row(self) -> bool:
        if self._selected_key_row < 0:
            return False
        snapshot = self._key_row_snapshot
        if snapshot is None:
            return False
        cur_name = self.key_name_edit.text().strip()
        cur_value = self.key_value_edit.text()
        orig_name, _orig_value = snapshot
        if cur_name != orig_name:
            return True
        if cur_value != "":
            return True
        return False

    def _on_key_apply(self) -> None:
        """Commit the form's values back to the credential store.

        Renames + value edits apply immediately via the
        :class:`CredentialStore`. The store raises on disk failure;
        we surface it via the form status label so the user can
        retry (rather than crashing the dialog).
        """
        row = self._selected_key_row
        if row < 0:
            return
        new_name = self.key_name_edit.text().strip()
        new_value_text = self.key_value_edit.text()
        if not new_name:
            QMessageBox.warning(
                self, "Missing name", "Key name cannot be empty."
            )
            return
        name_item = self.keys_table.item(row, 0)
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole)
            if name_item is not None
            else False
        )
        if is_default and new_name != "default":
            QMessageBox.warning(
                self,
                "Default row name",
                "The first row must be named 'default'. Pick another "
                "name to add a new key.",
            )
            return
        orig_name, orig_value = self._key_row_snapshot or ("", "")
        store = self._current_credential_store()
        try:
            if new_name != orig_name:
                existing = store.get(orig_name)
                if existing is not None:
                    store.set(new_name, existing)
                    store.delete(orig_name)
            if new_value_text:
                store.set(new_name, new_value_text)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Credential store error",
                f"Couldn't update key: {exc}",
            )
            return
        if name_item is not None:
            name_item.setText(new_name)
        value_item = self.keys_table.item(row, 1)
        if value_item is not None:
            if new_value_text:
                value_item.setText("•" * min(len(new_value_text), 16))
            else:
                value_item.setText(orig_value)
        # Clear the form value so the freshly-typed secret isn't
        # left in the widget. The name stays populated so the user
        # can keep editing it.
        self.key_value_edit.blockSignals(True)
        try:
            self.key_value_edit.setText("")
        finally:
            self.key_value_edit.blockSignals(False)
        new_value_display = value_item.text() if value_item is not None else ""
        self._key_row_snapshot = (new_name, new_value_display)
        self._refresh_key_apply_state()

    def _on_key_revert(self) -> None:
        snapshot = self._key_row_snapshot
        if snapshot is None:
            return
        name_text, _ = snapshot
        self.key_name_edit.blockSignals(True)
        self.key_value_edit.blockSignals(True)
        try:
            self.key_name_edit.setText(name_text)
            self.key_value_edit.setText("")
        finally:
            self.key_name_edit.blockSignals(False)
            self.key_value_edit.blockSignals(False)
        self._refresh_key_apply_state()

    def _on_key_add(self) -> None:
        existing = set(self._current_key_names())
        n = 1
        while f"key-{n}" in existing:
            n += 1
        self._append_key_row(f"key-{n}", "", is_default=False)
        new_row = self.keys_table.rowCount() - 1
        self.keys_table.selectRow(new_row)

    def _on_key_remove(self) -> None:
        """Delete the selected key from the credential store on disk."""
        row = self._selected_key_row
        if row <= 0:
            return
        name_item = self.keys_table.item(row, 0)
        if name_item is None:
            return
        name = name_item.text().strip()
        if name == "default":
            return
        try:
            self._current_credential_store().delete(name)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Credential store error",
                f"Couldn't delete key: {exc}",
            )
            return
        self.keys_table.removeRow(row)

    def _wrap_in_scroll(self, widget: QWidget, *, max_height: int):
        from src.ui.claude._qt_helpers import wrap_in_scroll

        return wrap_in_scroll(widget, max_height=max_height)