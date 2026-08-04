"""Providers tab mixin for the global settings dialog.

Ported from ``profile_editor/_providers_tab.py``. The provider
*registry* is now truly global (shared by every profile); each
profile's :class:`ModelEndpoint` references a provider by name.

Behavioural differences from the per-profile version:

- The provider list reads from / writes to ``self._settings.providers``
  (a ``dict[str, Provider]`` on :class:`GlobalSettings`) rather than
  the per-profile ``ClaudeProfile.providers``.
- The API Key combo is populated from the global :class:`CredentialStore`
  (passed in via the constructor), not the per-profile ``credential_store``
  attribute that the original mixin read from.
- The ``_refresh_endpoint_provider_combo`` calls from the original mixin
  (which refreshed the *per-profile* Model Configuration tab) are dropped
  — the model config tab is in a different dialog and gets refreshed
  the next time the profile editor opens.

The select-and-edit pattern is otherwise identical: ``QTableWidget`` is
read-only; the form below mirrors the selected row's name + URL +
key_ref + available_models, and the user clicks Apply to commit.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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
from src.profiles.models import Provider


class ProvidersTabMixin:
    def _build_providers_tab(self) -> None:
        """Tab 2: the global provider registry."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        prov_help = QLabel(
            "Named upstream providers shared by every profile. The "
            "<b>default</b> provider is always present and cannot be "
            "removed — it's the fall-through target for models that "
            "don't name a more specific provider. Select a row to edit, "
            "then click <b>Apply</b>. Add new providers with "
            "<b>Add…</b> and remove non-default ones with "
            "<b>Remove Selected</b>."
        )
        prov_help.setWordWrap(True)
        prov_help.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(prov_help)

        self.providers_table = QTableWidget(0, 4)
        self.providers_table.setHorizontalHeaderLabels(
            ["Name", "URL", "API Key", "Available Models"]
        )
        self.providers_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.providers_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.providers_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.providers_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.providers_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.providers_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.providers_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.providers_table.verticalHeader().setVisible(False)
        self.providers_table.itemSelectionChanged.connect(
            self._on_provider_selection_changed
        )
        self.providers_table.setMinimumHeight(140)
        self.providers_table_scroll = self._wrap_in_scroll(
            self.providers_table, max_height=180
        )
        tab_layout.addWidget(self.providers_table_scroll)

        prov_btns = QHBoxLayout()
        prov_add = QPushButton("Add…")
        prov_add.clicked.connect(self._on_provider_add)
        prov_remove = QPushButton("Remove Selected")
        prov_remove.clicked.connect(self._on_provider_remove)
        self.providers_remove_btn = prov_remove
        prov_btns.addWidget(prov_add)
        prov_btns.addWidget(prov_remove)
        prov_btns.addStretch(1)
        prov_btn_container = QWidget()
        prov_btn_container.setLayout(prov_btns)
        tab_layout.addWidget(prov_btn_container)

        # ---- Row editor form -------------------------------------------
        editor_box = QGroupBox("Edit selected provider")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        self._selected_provider_row: int = -1
        # Snapshot tuple shape: ``(name, url, key_ref,
        # available_models, is_default)``.
        self._provider_row_snapshot: (
            tuple[str, str, str | None, tuple[str, ...], bool] | None
        ) = None

        self.provider_name_edit = QLineEdit()
        self.provider_name_edit.setPlaceholderText("e.g. MiniMax")
        editor_layout.addRow("Provider Name:", self.provider_name_edit)

        self.provider_url_edit = QLineEdit()
        self.provider_url_edit.setPlaceholderText("https://api.example.com")
        editor_layout.addRow("Endpoint URL:", self.provider_url_edit)

        self.provider_key_combo = QComboBox()
        self.provider_key_combo.addItem("(no key)", None)
        editor_layout.addRow("API Key:", self.provider_key_combo)

        self.provider_models_edit = QLineEdit()
        self.provider_models_edit.setPlaceholderText(
            "comma-separated, e.g. claude-opus-4-8, claude-sonnet-4-6"
        )
        editor_layout.addRow("Available Models:", self.provider_models_edit)

        self.provider_name_edit.textChanged.connect(self._on_provider_form_changed)
        self.provider_url_edit.textChanged.connect(self._on_provider_form_changed)
        self.provider_key_combo.currentIndexChanged.connect(
            self._on_provider_form_changed
        )
        self.provider_models_edit.textChanged.connect(self._on_provider_form_changed)

        btn_row = QHBoxLayout()
        self.provider_apply_btn = QPushButton("Apply")
        self.provider_apply_btn.setEnabled(False)
        self.provider_apply_btn.clicked.connect(self._on_provider_apply)
        self.provider_revert_btn = QPushButton("Revert")
        self.provider_revert_btn.setEnabled(False)
        self.provider_revert_btn.clicked.connect(self._on_provider_revert)
        btn_row.addStretch(1)
        btn_row.addWidget(self.provider_apply_btn)
        btn_row.addWidget(self.provider_revert_btn)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        editor_layout.addRow("", btn_container)

        self.provider_form_status = QLabel("Select a row to edit")
        self.provider_form_status.setStyleSheet(
            "color: gray; font-size: 11px;"
        )
        editor_layout.addRow("", self.provider_form_status)

        self._set_provider_form_enabled(False)

        tab_layout.addWidget(editor_box)
        tab_layout.addStretch(1)

        self.tabs.addTab(tab, "Providers")

    def _populate_providers_tab(self) -> None:
        """Fill the providers table from ``self._settings.providers``."""
        self.providers_table.setRowCount(0)
        # Pinned default row — created if missing.
        default_provider = self._settings.providers.get("default", {})
        default_url = default_provider.get("url") or "https://api.example.com"
        default_key = default_provider.get("key_ref")
        default_models = tuple(default_provider.get("available_models") or ())
        self._append_provider_row(
            "default",
            default_url,
            key_ref=default_key,
            available_models=default_models,
            is_default=True,
        )
        for name, prov in self._settings.providers.items():
            if name == "default":
                continue
            if not isinstance(prov, dict):
                continue
            self._append_provider_row(
                name,
                prov.get("url") or "",
                key_ref=prov.get("key_ref"),
                available_models=tuple(prov.get("available_models") or ()),
                is_default=False,
            )

    def _collect_providers_tab(self) -> None:
        """Write the providers table back to ``self._settings.providers``.

        Stores providers as plain ``dict``s (not
        :class:`Provider` dataclass instances) because
        :func:`GlobalSettingsStore._serialize_providers` only
        serializes dicts — anything else is silently dropped on
        save. The deserializer accepts both shapes, so
        round-tripping via dict is safe.
        """
        out: dict[str, dict] = {}
        for row in range(self.providers_table.rowCount()):
            name_item = self.providers_table.item(row, 0)
            url_item = self.providers_table.item(row, 1)
            key_item = self.providers_table.item(row, 2)
            models_item = self.providers_table.item(row, 3)
            if name_item is None or url_item is None:
                continue
            name = name_item.text().strip()
            url = url_item.text().strip()
            if not name or not url:
                continue
            if name in out:
                continue
            key_ref: str | None = None
            if key_item is not None:
                stored = key_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(stored, str) and stored:
                    key_ref = stored
            available_models = self._parse_aliases(
                models_item.text() if models_item is not None else ""
            )
            entry: dict = {"url": url}
            if key_ref:
                entry["key_ref"] = key_ref
            if available_models:
                entry["available_models"] = list(available_models)
            out[name] = entry
        self._settings.providers = out

    def _current_credential_store(self) -> CredentialStore:
        """Return the credential store this dialog is editing.

        The dialog stores it on ``self._credential_store``; if not set
        (e.g. when the dialog is exercised standalone in tests), fall
        back to the default on-disk location.
        """
        from src.profiles.credential_store import CredentialStore
        from src.proxy.paths import data_dir

        store = getattr(self, "_credential_store", None)
        if store is None:
            store = CredentialStore(data_dir() / "credentials.json")
            self._credential_store = store
        return store

    def _current_key_names(self) -> list[str]:
        """Snapshot of credential names available globally."""
        try:
            refs = self._current_credential_store().all_refs()
        except (OSError, ValueError):
            return []
        return sorted(refs.keys())

    def _append_provider_row(
        self,
        name: str,
        url: str,
        key_ref: str | None = None,
        available_models: tuple[str, ...] = (),
        *,
        is_default: bool,
    ) -> None:
        row = self.providers_table.rowCount()
        self.providers_table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, is_default)
        self.providers_table.setItem(row, 0, name_item)
        url_item = QTableWidgetItem(url)
        url_item.setFlags(url_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 1, url_item)
        key_item = self._make_provider_key_item(key_ref)
        key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 2, key_item)
        models_item = QTableWidgetItem(self._format_aliases(available_models))
        models_item.setFlags(models_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 3, models_item)

    def _make_provider_key_item(self, ref: str | None) -> QTableWidgetItem:
        item = QTableWidgetItem()
        if ref is None:
            item.setText("(no key)")
            item.setData(Qt.ItemDataRole.UserRole, None)
        else:
            names = self._current_key_names()
            if ref in names:
                item.setText(ref)
            else:
                item.setText(f"{ref} (missing)")
                item.setBackground(Qt.GlobalColor.yellow)
            item.setData(Qt.ItemDataRole.UserRole, ref)
        return item

    def _repopulate_provider_key_combo_for_ref(
        self, ref: str | None
    ) -> None:
        if not hasattr(self, "provider_key_combo"):
            return
        self.provider_key_combo.blockSignals(True)
        try:
            self.provider_key_combo.clear()
            self.provider_key_combo.addItem("(no key)", None)
            for name in self._current_key_names():
                self.provider_key_combo.addItem(name, name)
            target_idx = 0
            for i in range(self.provider_key_combo.count()):
                if self.provider_key_combo.itemData(i) == ref:
                    target_idx = i
                    break
            self.provider_key_combo.setCurrentIndex(target_idx)
        finally:
            self.provider_key_combo.blockSignals(False)

    def _on_provider_selection_changed(self) -> None:
        sel = self.providers_table.selectionModel()
        row = (
            sel.selectedRows()[0].row() if sel and sel.selectedRows() else -1
        )
        self._selected_provider_row = row
        if row < 0:
            self._provider_row_snapshot = None
            self._set_provider_form_enabled(False)
            self.provider_form_status.setText("Select a row to edit")
            self._refresh_provider_apply_state()
            if hasattr(self, "providers_remove_btn"):
                self.providers_remove_btn.setEnabled(False)
            return
        name_item = self.providers_table.item(row, 0)
        url_item = self.providers_table.item(row, 1)
        key_item = self.providers_table.item(row, 2)
        models_item = self.providers_table.item(row, 3)
        name_text = name_item.text() if name_item is not None else ""
        url_text = url_item.text() if url_item is not None else ""
        key_ref: str | None = (
            key_item.data(Qt.ItemDataRole.UserRole)
            if key_item is not None
            else None
        )
        models_text = models_item.text() if models_item is not None else ""
        models = self._parse_aliases(models_text)
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole)
            if name_item is not None
            else False
        )
        self._set_provider_form_enabled(True)
        self.provider_name_edit.blockSignals(True)
        self.provider_url_edit.blockSignals(True)
        self.provider_key_combo.blockSignals(True)
        self.provider_models_edit.blockSignals(True)
        try:
            self.provider_name_edit.setText(name_text)
            self.provider_url_edit.setText(url_text)
            self._repopulate_provider_key_combo_for_ref(key_ref)
            self.provider_models_edit.setText(self._format_aliases(models))
        finally:
            self.provider_name_edit.blockSignals(False)
            self.provider_url_edit.blockSignals(False)
            self.provider_key_combo.blockSignals(False)
            self.provider_models_edit.blockSignals(False)
        self._provider_row_snapshot = (
            name_text, url_text, key_ref, models, is_default
        )
        self.provider_form_status.setText(
            f"Editing row {row + 1} of {self.providers_table.rowCount()}"
        )
        if hasattr(self, "providers_remove_btn"):
            self.providers_remove_btn.setEnabled(not is_default)
        self._refresh_provider_apply_state()

    def _set_provider_form_enabled(self, enabled: bool) -> None:
        self.provider_name_edit.setEnabled(enabled)
        self.provider_url_edit.setEnabled(enabled)
        self.provider_key_combo.setEnabled(enabled)
        self.provider_models_edit.setEnabled(enabled)
        if not enabled:
            self.provider_apply_btn.setEnabled(False)
            self.provider_revert_btn.setEnabled(False)

    def _on_provider_form_changed(self, *_args: object) -> None:
        if getattr(self, "_selected_provider_row", -1) < 0:
            return
        self._refresh_provider_apply_state()

    def _refresh_provider_apply_state(self) -> None:
        divergent = self._provider_form_values_diverge_from_row()
        self.provider_apply_btn.setEnabled(divergent)
        self.provider_revert_btn.setEnabled(divergent)
        if divergent:
            self.provider_form_status.setText(
                "Edited — click Apply to save, or Revert to discard"
            )
        elif self._selected_provider_row >= 0:
            self.provider_form_status.setText(
                f"Row {self._selected_provider_row + 1} — no pending changes"
            )

    def _provider_form_values_diverge_from_row(self) -> bool:
        if self._selected_provider_row < 0:
            return False
        snapshot = self._provider_row_snapshot
        if snapshot is None:
            return False
        cur = (
            self.provider_name_edit.text().strip(),
            self.provider_url_edit.text().strip(),
            self.provider_key_combo.currentData(),
            self._parse_aliases(self.provider_models_edit.text()),
        )
        return cur != (snapshot[0], snapshot[1], snapshot[2], snapshot[3])

    def _on_provider_apply(self) -> None:
        row = self._selected_provider_row
        if row < 0:
            return
        new_name = self.provider_name_edit.text().strip()
        new_url = self.provider_url_edit.text().strip()
        new_models = self._parse_aliases(self.provider_models_edit.text())
        new_key: str | None = self.provider_key_combo.currentData()
        if not new_name:
            QMessageBox.warning(
                self, "Missing name", "Provider name cannot be empty."
            )
            return
        if not new_url:
            QMessageBox.warning(
                self, "Missing URL", "Endpoint URL cannot be empty."
            )
            return
        name_item = self.providers_table.item(row, 0)
        url_item = self.providers_table.item(row, 1)
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole)
            if name_item is not None
            else False
        )
        if is_default and new_name != "default":
            QMessageBox.warning(
                self,
                "Default provider name",
                "The first row must be named 'default'. Pick another "
                "name to add a new provider.",
            )
            return
        if name_item is not None:
            name_item.setText(new_name)
        if url_item is not None:
            url_item.setText(new_url)
        key_item = self._make_provider_key_item(new_key)
        key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 2, key_item)
        models_item = self.providers_table.item(row, 3)
        if models_item is None:
            models_item = QTableWidgetItem()
            models_item.setFlags(
                models_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            self.providers_table.setItem(row, 3, models_item)
        models_item.setText(self._format_aliases(new_models))
        self.provider_models_edit.blockSignals(True)
        try:
            self.provider_models_edit.setText(self._format_aliases(new_models))
        finally:
            self.provider_models_edit.blockSignals(False)
        self._provider_row_snapshot = (
            new_name, new_url, new_key, new_models, is_default,
        )
        self._refresh_provider_apply_state()

    def _on_provider_revert(self) -> None:
        snapshot = self._provider_row_snapshot
        if snapshot is None:
            return
        name_text, url_text, key_ref, models, _ = snapshot
        self.provider_name_edit.blockSignals(True)
        self.provider_url_edit.blockSignals(True)
        self.provider_key_combo.blockSignals(True)
        self.provider_models_edit.blockSignals(True)
        try:
            self.provider_name_edit.setText(name_text)
            self.provider_url_edit.setText(url_text)
            self._repopulate_provider_key_combo_for_ref(key_ref)
            self.provider_models_edit.setText(self._format_aliases(models))
        finally:
            self.provider_name_edit.blockSignals(False)
            self.provider_url_edit.blockSignals(False)
            self.provider_key_combo.blockSignals(False)
            self.provider_models_edit.blockSignals(False)
        self._refresh_provider_apply_state()

    def _on_provider_add(self) -> None:
        existing = {
            self.providers_table.item(r, 0).text().strip()
            for r in range(self.providers_table.rowCount())
        }
        n = 1
        while f"provider-{n}" in existing:
            n += 1
        self._append_provider_row(f"provider-{n}", "", is_default=False)
        new_row = self.providers_table.rowCount() - 1
        self.providers_table.selectRow(new_row)

    def _on_provider_remove(self) -> None:
        row = self._selected_provider_row
        if row <= 0:
            return
        name_item = self.providers_table.item(row, 0)
        if name_item is None:
            return
        if name_item.data(Qt.ItemDataRole.UserRole):
            return
        self.providers_table.removeRow(row)

    # ---- shared helpers (also used by the API Keys tab) -----------------

    def _parse_aliases(self, text: str) -> tuple[str, ...]:
        """Parse comma-separated text into a deduped tuple.

        Empty / whitespace-only entries are dropped. Order is preserved
        (left-to-right after dedup). Used identically on the
        available_models field here and the aliases field on the
        profile editor's Endpoints tab.
        """
        if not text:
            return ()
        parts = [s.strip() for s in text.split(",")]
        return tuple(dict.fromkeys(p for p in parts if p))

    def _format_aliases(self, aliases: tuple[str, ...]) -> str:
        """Inverse of :meth:`_parse_aliases`: tuple → comma-separated string."""
        return ", ".join(aliases) if aliases else ""

    def _wrap_in_scroll(
        self, widget: QWidget, *, max_height: int
    ):
        from src.ui.claude._qt_helpers import wrap_in_scroll

        return wrap_in_scroll(widget, max_height=max_height)

    def _make_tab(self, inner: QWidget) -> QWidget:
        from src.ui.claude._qt_helpers import make_tab

        return make_tab(inner)