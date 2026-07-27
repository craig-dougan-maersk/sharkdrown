from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui.claude.profile_editor._dialogs import StandardEnvVarsDialog


class EnvVarsTabMixin:
    def _build_env_vars_tab(self) -> None:
        """Tab 4: extra env vars exported to ``claude`` sessions.

        Same select-and-edit pattern as the other tabs: the
        ``QTableWidget`` is read-only and shows the current rows;
        the form below mirrors the selected row's name + value and
        the user clicks Apply to commit any change.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        env_help = QLabel(
            "Extra environment variables exported to <code>claude</code> for "
            "sessions of this profile. Select a row below to edit its "
            "name and value, then click <b>Apply</b>. "
            "<em>ANTHROPIC_BASE_URL</em>, <em>CLAUDE_CONFIG_DIR</em> "
            "and <em>ANTHROPIC_API_KEY</em> are owned by the "
            "integration and cannot be overridden here."
        )
        env_help.setWordWrap(True)
        env_help.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(env_help)

        self.env_table = QTableWidget(0, 2)
        self.env_table.setHorizontalHeaderLabels(["Name", "Value"])
        self.env_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.env_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.env_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.env_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.env_table.itemSelectionChanged.connect(self._on_env_selection_changed)
        # Scroll-wrap the table to keep the layout compact.
        self.env_table.setMinimumHeight(96)
        self.env_table_scroll = self._wrap_in_scroll(self.env_table, max_height=180)
        tab_layout.addWidget(self.env_table_scroll)

        env_btns = QHBoxLayout()
        env_add_standard = QPushButton("Add Standard…")
        env_add_standard.setToolTip(
            "Pick one or more Claude Code env vars from the curated catalog."
        )
        env_add_standard.clicked.connect(self._on_env_add_standard)
        env_add_custom = QPushButton("Add Custom…")
        env_add_custom.setToolTip(
            "Add a free-form env var (variable name + value)."
        )
        env_add_custom.clicked.connect(self._on_env_add_custom)
        env_remove = QPushButton("Remove Selected")
        env_remove.clicked.connect(lambda: self._remove_selected_rows(self.env_table))
        env_btns.addWidget(env_add_standard)
        env_btns.addWidget(env_add_custom)
        env_btns.addWidget(env_remove)
        env_btns.addStretch(1)
        env_btn_container = QWidget()
        env_btn_container.setLayout(env_btns)
        tab_layout.addWidget(env_btn_container)

        # ---- Env var editor form ---------------------------------------
        from PySide6.QtWidgets import QFormLayout, QGroupBox

        editor_box = QGroupBox("Edit selected variable")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        self._selected_env_row: int = -1
        self._env_row_snapshot: tuple[str, str] | None = None

        self.env_name_edit = QLineEdit()
        self.env_name_edit.setPlaceholderText("VARIABLE_NAME")
        editor_layout.addRow("Name:", self.env_name_edit)

        self.env_value_edit = QLineEdit()
        self.env_value_edit.setPlaceholderText("value")
        editor_layout.addRow("Value:", self.env_value_edit)

        self.env_name_edit.textChanged.connect(self._on_env_form_changed)
        self.env_value_edit.textChanged.connect(self._on_env_form_changed)

        btn_row = QHBoxLayout()
        self.env_apply_btn = QPushButton("Apply")
        self.env_apply_btn.setEnabled(False)
        self.env_apply_btn.clicked.connect(self._on_env_apply)
        self.env_revert_btn = QPushButton("Revert")
        self.env_revert_btn.setEnabled(False)
        self.env_revert_btn.clicked.connect(self._on_env_revert)
        btn_row.addStretch(1)
        btn_row.addWidget(self.env_apply_btn)
        btn_row.addWidget(self.env_revert_btn)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        editor_layout.addRow("", btn_container)

        self.env_form_status = QLabel("Select a row to edit")
        self.env_form_status.setStyleSheet("color: gray; font-size: 11px;")
        editor_layout.addRow("", self.env_form_status)

        self._set_env_form_enabled(False)

        tab_layout.addWidget(editor_box)
        tab_layout.addStretch(1)

        self.tabs.addTab(tab, "Environment Variables")

    def _on_env_selection_changed(self) -> None:
        """Mirror the currently-selected env var row into the form."""
        sel = self.env_table.selectionModel()
        row = sel.selectedRows()[0].row() if sel and sel.selectedRows() else -1
        self._selected_env_row = row
        if row < 0:
            self._env_row_snapshot = None
            self._set_env_form_enabled(False)
            self.env_form_status.setText("Select a row to edit")
            self._refresh_env_apply_state()
            return
        name_item = self.env_table.item(row, 0)
        value_item = self.env_table.item(row, 1)
        name_text = name_item.text() if name_item is not None else ""
        value_text = value_item.text() if value_item is not None else ""
        # Strip the catalog " * already-added" marker if present
        # (carried over from older code paths).
        if name_text.endswith("*"):
            name_text = name_text[: -len("  *")]
        self._set_env_form_enabled(True)
        self.env_name_edit.blockSignals(True)
        self.env_value_edit.blockSignals(True)
        try:
            self.env_name_edit.setText(name_text)
            self.env_value_edit.setText(value_text)
        finally:
            self.env_name_edit.blockSignals(False)
            self.env_value_edit.blockSignals(False)
        self._env_row_snapshot = (name_text, value_text)
        self.env_form_status.setText(
            f"Editing row {row + 1} of {self.env_table.rowCount()}"
        )
        self._refresh_env_apply_state()

    def _set_env_form_enabled(self, enabled: bool) -> None:
        self.env_name_edit.setEnabled(enabled)
        self.env_value_edit.setEnabled(enabled)
        if not enabled:
            self.env_apply_btn.setEnabled(False)
            self.env_revert_btn.setEnabled(False)

    def _on_env_form_changed(self, *_args: object) -> None:
        if getattr(self, "_selected_env_row", -1) < 0:
            return
        self._refresh_env_apply_state()

    def _refresh_env_apply_state(self) -> None:
        divergent = self._env_form_values_diverge_from_row()
        self.env_apply_btn.setEnabled(divergent)
        self.env_revert_btn.setEnabled(divergent)
        if divergent:
            self.env_form_status.setText(
                "Edited — click Apply to save, or Revert to discard"
            )
        elif self._selected_env_row >= 0:
            self.env_form_status.setText(
                f"Row {self._selected_env_row + 1} — no pending changes"
            )

    def _env_form_values_diverge_from_row(self) -> bool:
        if self._selected_env_row < 0:
            return False
        snapshot = self._env_row_snapshot
        if snapshot is None:
            return False
        cur = (
            self.env_name_edit.text().strip(),
            self.env_value_edit.text(),
        )
        # ``name`` is matched trimmed; ``value`` is matched verbatim
        # so an empty form value can be intentionally identical to
        # the row's empty value.
        return cur[0] != snapshot[0].strip() or cur[1] != snapshot[1]

    def _on_env_apply(self) -> None:
        row = self._selected_env_row
        if row < 0:
            return
        new_name = self.env_name_edit.text().strip()
        new_value = self.env_value_edit.text()
        if not new_name:
            QMessageBox.warning(
                self, "Missing name", "Variable name cannot be empty.",
            )
            return
        if self.env_table.item(row, 0) is not None:
            self.env_table.item(row, 0).setText(new_name)
        if self.env_table.item(row, 1) is not None:
            self.env_table.item(row, 1).setText(new_value)
        # Refresh baseline so the form is no longer divergent.
        self._env_row_snapshot = (new_name, new_value)
        self._refresh_env_apply_state()

    def _on_env_revert(self) -> None:
        snapshot = self._env_row_snapshot
        if snapshot is None:
            return
        name_text, value_text = snapshot
        self.env_name_edit.blockSignals(True)
        self.env_value_edit.blockSignals(True)
        try:
            self.env_name_edit.setText(name_text)
            self.env_value_edit.setText(value_text)
        finally:
            self.env_name_edit.blockSignals(False)
            self.env_value_edit.blockSignals(False)
        self._refresh_env_apply_state()

    # ---- env vars add slots -------------------------------------------

    def _existing_env_keys(self) -> frozenset[str]:
        """Snapshot of variable names currently in the env table.

        Used by the standard picker to flag already-added entries.
        Strips the ``*`` "already added" marker defensively in case
        a previous round of edits left it behind (it shouldn't —
        the picker re-renders on each open — but cheap to handle).
        """
        from src.profiles.env_var_catalog import standard_env_var_names

        keys: set[str] = set()
        for row in range(self.env_table.rowCount()):
            k_item = self.env_table.item(row, 0)
            if k_item is None:
                continue
            name = k_item.text().strip()
            if name.endswith("*"):
                name = name[: -len("  *")]
            if name and name in standard_env_var_names():
                keys.add(name)
        return frozenset(keys)

    def _on_env_add_standard(self) -> None:
        """Open the curated catalog picker; insert picks without values."""
        dlg = StandardEnvVarsDialog(
            already_added=self._existing_env_keys(),
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        for name in dlg.selected_names():
            # Insert a row with the name pre-filled and the value
            # blank. ``_table_pairs`` drops blank-value rows on save,
            # so the user *must* type a value before the variable
            # actually persists — that matches the "pick then fill
            # in the value" workflow the user asked for.
            self._append_table_row(self.env_table, name, "")

    def _on_env_add_custom(self) -> None:
        """Insert a blank row for a free-form env var.

        Same shape as the original "Add…" button: a (name, value)
        pair the user fills in directly. The ``Standard…`` button is
        the curated alternative.
        """
        self._append_table_row(self.env_table, "", "")
