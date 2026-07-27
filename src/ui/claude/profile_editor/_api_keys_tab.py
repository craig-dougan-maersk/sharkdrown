"""API Keys tab mixin for :class:`ProfileEditorDialog`.

Extracted from ``profile_editor_dialog.py`` verbatim. This mixin
assumes ``self`` has the same attributes as ``ProfileEditorDialog``
(``keys_table``, ``_creds``, ``_cred_edited_by_key``, ``_key_renames``,
``_initial_key_names``, ``_populating``, etc.).
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


class ApiKeysTabMixin:
    def _build_api_key_tab(self) -> None:
        """Tab 3: list of named API keys, with a row-select + form editor.

        The table is read-only. Clicking a row populates the form
        below with that row's *name* and *value*. The user can
        change either and click Apply — or Revert. Apply is enabled
        only when the form's values diverge from the selected row.

        The ``default`` row (always row 0) is *selectable* and
        *editable* in the form — the user's complaint was that the
        default key's value couldn't be edited conveniently. Names
        are renamed on Apply; the API Key dropdown on the Model
        Endpoints tab tracks the new names via the keys table
        refresh.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        cred_help = QLabel(
            "Named API keys stored in <code>credentials.json</code> (chmod 0600). "
            "The <b>default</b> key is always present and cannot be removed. "
            "Select a row to edit its name and value, then click "
            "<b>Apply</b>. Other keys can be added with <b>Add…</b> "
            "and removed with <b>Remove Selected</b>."
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
        self.keys_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.keys_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.keys_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.keys_table.verticalHeader().setVisible(False)
        self.keys_table.itemSelectionChanged.connect(
            self._on_key_selection_changed
        )
        self.keys_table.setMinimumHeight(140)
        self.keys_table_scroll = self._wrap_in_scroll(self.keys_table, max_height=180)
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

        # ---- Row editor form -------------------------------------------
        # ``Name`` (editable, validated to non-empty on Apply) +
        # ``Value`` (password-mode QLineEdit). Apply enabled only
        # when the form's values differ from the selected row.
        from PySide6.QtWidgets import QFormLayout, QGroupBox

        editor_box = QGroupBox("Edit selected key")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        # Index of the currently selected row in ``keys_table``,
        # ``-1`` if no selection. Tracks the row whose data the
        # form mirrors.
        self._selected_key_row: int = -1
        # Snapshot of the row's values at form-load time.
        # ``None`` = no row selected.
        self._key_row_snapshot: tuple[str, str] | None = None

        self.key_name_edit = QLineEdit()
        self.key_name_edit.setPlaceholderText("e.g. work")
        editor_layout.addRow("Name:", self.key_name_edit)

        self.key_value_edit = QLineEdit()
        self.key_value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_value_edit.setPlaceholderText("(leave blank to keep existing value)")
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
        self.key_form_status.setStyleSheet("color: gray; font-size: 11px;")
        editor_layout.addRow("", self.key_form_status)

        self._set_key_form_enabled(False)

        tab_layout.addWidget(editor_box)
        tab_layout.addStretch(1)

        self.tabs.addTab(tab, "API Keys")

    def _populate_keys_table(self) -> None:
        """Fill the keys table from the CredentialStore snapshot.

        The ``default`` row is always row 0 (created if missing). Other
        keys follow in the order returned by ``all_refs()``. The
        value column shows ``••••••••`` for stored keys (masking the
        secret) and ``"(not set)"`` for newly-added rows.
        """
        self.keys_table.setRowCount(0)
        all_refs = self._creds.all_refs()
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
        """Insert a key row at the bottom of the table.

        ``value`` is the raw credential text — we render the value
        column as a mask so secrets aren't displayed on screen. The
        row is fully read-only; the form below is the editor.
        ``UserRole`` carries the ``is_default`` flag for cheap lookup.
        """
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
        """Snapshot of names currently in the keys table.

        Skips empty cells (which appear transiently while the user is
        renaming). The list is ordered: ``default`` first, then the
        rest in their current table order.
        """
        names: list[str] = []
        for row in range(self.keys_table.rowCount()):
            item = self.keys_table.item(row, 0)
            if item is None:
                continue
            name = item.text().strip()
            if name and name not in names:
                names.append(name)
        return names

    def _refresh_key_editor_label(self) -> None:
        # Removed in the select-and-edit refactor — the form's name
        # and value fields are populated by ``_on_key_selection_changed``
        # and updated on Apply. Kept as a no-op stub so any leftover
        # call site doesn't crash on the attribute name alone.
        return None

    def _append_table_row(self, table: QTableWidget, k: str, v: str) -> None:
        """Insert one row, marking cells read-only if the table is the
        env vars table. (Used for the env vars tab only — the keys
        table has its own ``_append_key_row``.)
        """
        row = table.rowCount()
        table.insertRow(row)
        k_item = QTableWidgetItem(k)
        k_item.setFlags(k_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, k_item)
        v_item = QTableWidgetItem(v)
        v_item.setFlags(v_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 1, v_item)
        # Auto-select the new row so the form below is populated.
        if table is self.env_table:
            table.selectRow(row)

    def _remove_selected_rows(self, table: QTableWidget) -> None:
        rows = sorted({i.row() for i in table.selectedIndexes()}, reverse=True)
        for r in rows:
            table.removeRow(r)

    def _on_cred_value_changed(self, _text: str) -> None:
        # Kept as a no-op for backwards compatibility with anything
        # still connecting to ``cred_value_edit.textChanged``. The new
        # flow tracks edits per-row via ``_on_key_form_changed``.
        pass

    # ---- API Keys tab slots -------------------------------------------

    def _on_key_selection_changed(self) -> None:
        """Mirror the currently-selected key's name + value into the form.

        Triggered by ``itemSelectionChanged``. Apply/Revert are
        disabled initially (the form freshly mirrors the row's
        values, so Apply would have nothing to do).
        """
        sel = self.keys_table.selectionModel()
        row = sel.selectedRows()[0].row() if sel and sel.selectedRows() else -1
        self._selected_key_row = row
        if row < 0:
            self._key_row_snapshot = None
            self._set_key_form_enabled(False)
            self.key_form_status.setText("Select a row to edit")
            self._refresh_key_apply_state()
            # The Remove button only acts on a real selection; default
            # row is non-removable regardless.
            if hasattr(self, "keys_remove_btn"):
                self.keys_remove_btn.setEnabled(False)
            return
        # Read selected row's name + value.
        name_item = self.keys_table.item(row, 0)
        value_item = self.keys_table.item(row, 1)
        name_text = name_item.text() if name_item is not None else ""
        value_text = value_item.text() if value_item is not None else ""
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole) if name_item is not None else False
        )
        self._set_key_form_enabled(True)
        self.key_name_edit.blockSignals(True)
        self.key_value_edit.blockSignals(True)
        try:
            self.key_name_edit.setText(name_text)
            # The ``Value`` cell displays a mask like "••••••••" —
            # we leave the form blank so the user typing replaces
            # the existing value (or leaving blank keeps it).
            self.key_value_edit.setText("")
        finally:
            self.key_name_edit.blockSignals(False)
            self.key_value_edit.blockSignals(False)
        self._key_row_snapshot = (name_text, value_text)
        self.key_form_status.setText(
            f"Editing row {row + 1} of {self.keys_table.rowCount()}"
        )
        # The Remove button is disabled for the default row.
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
        """Re-evaluate divergence after a name or value edit."""
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
        """``True`` when the form's name differs from the selected row's name,
        or the value has been typed (even to an empty string). Empty
        value means "no change" by convention.
        """
        if self._selected_key_row < 0:
            return False
        snapshot = self._key_row_snapshot
        if snapshot is None:
            return False
        cur_name = self.key_name_edit.text().strip()
        cur_value = self.key_value_edit.text()
        orig_name, orig_value = snapshot
        if cur_name != orig_name:
            return True
        # A non-empty value typed into the form counts as a divergence;
        # an empty form value is treated as "leave existing alone".
        if cur_value != "":
            return True
        return False

    def _on_key_apply(self) -> None:
        """Commit the form's values back to the selected key row.

        Renames are tracked via ``_key_renames`` so ``_on_accept``
        can rename the credential on disk. Value edits mark the
        new name in ``_cred_edited_by_key`` so the new value is
        written to the store on OK.
        """
        row = self._selected_key_row
        if row < 0:
            return
        new_name = self.key_name_edit.text().strip()
        new_value_text = self.key_value_edit.text()
        if not new_name:
            QMessageBox.warning(self, "Missing name", "Key name cannot be empty.")
            return
        # Default row name must remain "default".
        name_item = self.keys_table.item(row, 0)
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole) if name_item is not None else False
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
        # Detect rename: name changed.
        if new_name != orig_name:
            self._key_renames[new_name] = orig_name
        # Detect value edit: user typed something new. Store the
        # actual typed text so ``_on_accept`` can apply it without
        # re-reading the form (which only sees the *currently
        # selected* row).
        if new_value_text:
            self._cred_edited_by_key[new_name] = new_value_text
        # Update the row's display items.
        if name_item is not None:
            name_item.setText(new_name)
        value_item = self.keys_table.item(row, 1)
        if value_item is not None:
            if new_value_text:
                value_item.setText("•" * min(len(new_value_text), 16))
            else:
                value_item.setText(orig_value)  # unchanged
        # Re-snapshot to the new baseline. Clear the form's value
        # field so the freshly-typed secret isn't left in the
        # widget (the convention: the form is "empty" until the
        # user types a new value; leaving the typed text in place
        # would make Apply re-enable on a no-op edit). The name
        # remains populated so the user can keep editing it.
        self.key_value_edit.blockSignals(True)
        try:
            self.key_value_edit.setText("")
        finally:
            self.key_value_edit.blockSignals(False)
        new_value_display = value_item.text() if value_item is not None else ""
        self._key_row_snapshot = (new_name, new_value_display)
        # If the name changed, endpoints tables need to know — refresh
        # the API Key column on every endpoint row so the display
        # text follows.
        if new_name != orig_name or new_value_text:
            self._refresh_endpoint_key_columns()
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
        """Append a new key row with a placeholder name and select it."""
        existing = set(self._current_key_names())
        n = 1
        while f"key-{n}" in existing:
            n += 1
        self._append_key_row(f"key-{n}", "", is_default=False)
        new_row = self.keys_table.rowCount() - 1
        self.keys_table.selectRow(new_row)
        # The selection-changed handler takes care of refreshing
        # the endpoint tables via ``_refresh_endpoint_key_columns``.
        self._refresh_endpoint_key_columns()

    def _on_key_remove(self) -> None:
        """Delete the selected key row, with a guard for the default row."""
        row = self._selected_key_row
        if row <= 0:
            return
        name_item = self.keys_table.item(row, 0)
        if name_item is None:
            return
        name = name_item.text().strip()
        self.keys_table.removeRow(row)
        # Selection moves to the previous row (or invalid if none);
        # the form is reset by ``_on_key_selection_changed``.
        self._refresh_endpoint_key_columns()
        # Mark the deletion in ``_cred_edited_by_key`` so ``_on_accept``
        # deletes the credential from the store.
        self._cred_edited_by_key[f"__deleted__{name}"] = True
