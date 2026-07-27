"""Mixin: Providers tab — table + row-editor form for named upstream providers."""

from __future__ import annotations

import dataclasses

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.models import ClaudeProfile, ModelEndpoint, Provider


class ProvidersTabMixin:
    def _build_providers_tab(self) -> None:
        """Tab: a list of named upstream providers with their URLs.

        Same select-and-edit pattern as the API Keys and Env Vars
        tabs: the ``QTableWidget`` is read-only (no in-place Qt cell
        editor); the form below mirrors the selected row's *name*
        and *URL*, and the user clicks **Apply** to commit any
        change.

        The ``default`` row is always pinned at row 0 — every
        profile must have a ``default`` provider (the "fall-through"
        target for models that don't name a more specific provider).
        The ``default`` row's *name* is structurally fixed; only the
        URL is editable.

        The Providers tab is also the source of truth for the
        Model Configuration tab's per-row "Provider" combo: when the
        providers list changes, ``_refresh_endpoint_provider_combo``
        rebuilds that combo so the user picks from the current set.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        prov_help = QLabel(
            "Named upstream providers (e.g. <b>Anthropic</b>, <b>MiniMax</b>, "
            "<b>CoPilot</b>) and their base URL. The <b>default</b> "
            "provider is always present and cannot be removed — it's the "
            "fall-through target for models that don't name a more "
            "specific provider. Select a row to edit its name and URL, "
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
        self.providers_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.providers_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.providers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        from PySide6.QtWidgets import QFormLayout, QGroupBox

        editor_box = QGroupBox("Edit selected provider")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        self._selected_provider_row: int = -1
        # Snapshot of the row's values at form-load time so we can
        # detect divergence cheaply. Tuple shape:
        # ``(name, url, key_ref, available_models, is_default)``.
        # ``key_ref`` is the CredentialStore ref the proxy will use
        # to authenticate requests routed through this provider;
        # ``available_models`` is a tuple of model-name strings the
        # Model Configuration dropdown constrains itself to.
        self._provider_row_snapshot: (
            tuple[str, str, str | None, tuple[str, ...], bool] | None
        ) = None

        self.provider_name_edit = QLineEdit()
        self.provider_name_edit.setPlaceholderText("e.g. MiniMax")
        editor_layout.addRow("Provider Name:", self.provider_name_edit)

        self.provider_url_edit = QLineEdit()
        self.provider_url_edit.setPlaceholderText("https://api.example.com")
        editor_layout.addRow("Endpoint URL:", self.provider_url_edit)

        # API Key — combo populated from the CredentialStore. The
        # ``currentData()`` carries the ref string (or ``None`` for
        # the "(no key)" entry). The plain-text value lives in
        # CredentialStore, never on the profile, so the editor
        # displays a friendlier name (or the warning "(no key)")
        # without leaking the secret.
        self.provider_key_combo = QComboBox()
        self.provider_key_combo.addItem("(no key)", None)
        editor_layout.addRow("API Key:", self.provider_key_combo)

        # Available Models — comma-separated text, parsed by the
        # same ``_parse_aliases`` helper (it's the same shape:
        # strip / drop empties / dedupe). The form's value is the
        # *editor representation*; the row's column-3 cell renders
        # the canonical form too, so reading either side gives the
        # same normalised tuple.
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
        self.provider_form_status.setStyleSheet("color: gray; font-size: 11px;")
        editor_layout.addRow("", self.provider_form_status)

        self._set_provider_form_enabled(False)

        tab_layout.addWidget(editor_box)
        tab_layout.addStretch(1)

        self.tabs.addTab(tab, "Providers")

    def _append_provider_row(
        self,
        name: str,
        url: str,
        key_ref: str | None = None,
        available_models: tuple[str, ...] = (),
        *,
        is_default: bool,
    ) -> None:
        """Insert one read-only provider row.

        The row carries four ``QTableWidgetItem``s — Name, URL,
        API Key, Available Models — all read-only. ``UserRole``
        on the name cell carries the ``is_default`` flag so the
        form's Apply / Remove logic can cheaply identify the
        pinned row. The API Key column's ``UserRole`` carries the
        raw ``key_ref`` so selection-changed logic can rebuild
        the form's combo.
        """
        row = self.providers_table.rowCount()
        self.providers_table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, is_default)
        self.providers_table.setItem(row, 0, name_item)
        url_item = QTableWidgetItem(url)
        url_item.setFlags(url_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 1, url_item)
        # API Key cell — display name via the shared helper, with
        # ``UserRole`` carrying the ref string.
        key_item = self._make_provider_key_item(key_ref)
        key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 2, key_item)
        # Available Models cell — comma-separated text. The
        # canonical tuple lives on ``Provider.available_models``;
        # the cell text is the display form (parsed back to a
        # tuple on read by ``_collect_providers``).
        models_item = QTableWidgetItem(self._format_aliases(available_models))
        models_item.setFlags(models_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 3, models_item)

    def _make_provider_key_item(self, ref: str | None) -> QTableWidgetItem:
        """Build the Providers-tab API Key column cell.

        Mirror of ``_make_endpoint_key_item`` for the providers
        table — display text is the friendly key name (or
        ``"(no key)"``); the ``UserRole`` userData holds the
        actual ``key_ref`` (or ``None``). Stale refs are flagged
        with a yellow background so the user notices them on the
        provider row.
        """
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
        """Repopulate the Providers-tab API Key ``QComboBox``.

        Clears and refills with ``(no key)`` + every defined
        credential name, then selects the entry whose
        ``currentData()`` matches ``ref``. If no such entry
        exists, falls back to index 0 (``(no key)``).
        """
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

    def _populate_providers_table(self) -> None:
        """Fill the providers table from ``self._profile.providers``.

        The ``default`` row is pinned at row 0 (created if missing);
        the rest follow in their on-disk order. A profile that
        somehow ends up with no default provider is auto-seeded
        with a placeholder URL so the form's other tabs have
        something to route through. The user can replace the URL
        on the Providers tab — Apply refuses an empty URL.
        """
        self.providers_table.setRowCount(0)
        # Pinned default row — created if missing.
        default_provider = self._profile.providers.get("default")
        default_url = (
            default_provider.url
            if default_provider and default_provider.url
            else "https://api.example.com"
        )
        default_key = (
            default_provider.key_ref
            if default_provider is not None
            else None
        )
        default_models = (
            tuple(default_provider.available_models)
            if default_provider is not None and default_provider.available_models
            else ()
        )
        self._append_provider_row(
            "default",
            default_url,
            key_ref=default_key,
            available_models=default_models,
            is_default=True,
        )
        for name, provider in self._profile.providers.items():
            if name == "default":
                continue
            self._append_provider_row(
                name,
                provider.url,
                key_ref=provider.key_ref,
                available_models=tuple(provider.available_models or ()),
                is_default=False,
            )

    def _maybe_seed_endpoints_from_provider_catalog(self) -> None:
        """Auto-seed ``model_endpoints`` from provider catalogs.

        A common pitfall: the user fills out a Provider on the
        Providers tab (URL + API key + Available Models catalog)
        but never adds the matching rows on the Model Configuration
        tab. The result is a profile with zero routable endpoints —
        and ``settings.json.availableModels`` ends up empty, so
        Claude Code's ``/model`` shows only the hardcoded built-in
        default rather than anything from the user's profile.

        Closing this gap at dialog-open time is cheaper than trying
        to teach users the two-step workflow explicitly: when the
        in-memory profile has empty ``model_endpoints`` *and* at
        least one provider exposes a non-empty ``available_models``
        catalog, fill in one ``ModelEndpoint(provider_name=p,
        aliases=())`` row per ``(provider, model)`` pair.

        Idempotent: if the user already has rows, this is a no-op.
        Mutates ``self._profile.model_endpoints`` (a deepcopy of the
        caller's profile, so the original on-disk record isn't
        touched — the seeded rows become real only when ``OK``
        commits via ``_on_accept``).
        """
        # Idempotent: only seed when the profile has zero endpoints.
        if self._profile.model_endpoints:
            return
        seeded: dict[str, ModelEndpoint] = {}
        for prov_name, provider in self._profile.providers.items():
            catalog = tuple(getattr(provider, "available_models", ()) or ())
            if not catalog:
                continue
            for model in catalog:
                if not isinstance(model, str) or not model.strip():
                    continue
                # If two providers each host the same model name,
                # the first wins (the editor's ``_on_accept`` doesn't
                # enforce catalog uniqueness today, so we mirror the
                # existing tolerance rather than rejecting here).
                if model in seeded:
                    continue
                seeded[str(model)] = ModelEndpoint(
                    provider_name=str(prov_name),
                    aliases=(),
                )
        if seeded:
            self._profile = dataclasses.replace(
                self._profile, model_endpoints=seeded
            )

    def _current_provider_names(self) -> list[str]:
        """Snapshot of provider names currently in the providers table.

        ``default`` is always first (it's pinned at row 0); the rest
        follow in their current table order. Used to populate the
        Model Configuration tab's per-row Provider combo.
        """
        names: list[str] = []
        for row in range(self.providers_table.rowCount()):
            item = self.providers_table.item(row, 0)
            if item is None:
                continue
            name = item.text().strip()
            if name and name not in names:
                names.append(name)
        return names

    def _current_provider_url(self, name: str) -> str | None:
        """URL of the named provider, or ``None`` if no such provider."""
        target = name.strip()
        if not target:
            return None
        for row in range(self.providers_table.rowCount()):
            item = self.providers_table.item(row, 0)
            if item is None or item.text().strip() != target:
                continue
            url_item = self.providers_table.item(row, 1)
            if url_item is None:
                return None
            return url_item.text().strip()
        return None

    def _on_provider_selection_changed(self) -> None:
        """Mirror the currently-selected provider's row into the form.

        Triggered by ``itemSelectionChanged``. Reads the four row
        cells — Name, URL, API Key (ref via ``UserRole``),
        Available Models (comma-joined text) — and mirrors them into
        the form's four fields. The ``default`` row's name is
        structurally fixed (refused at Apply) but its URL and the
        rest of the form fields remain editable. Apply/Revert start
        disabled — the form freshly mirrors the row, so divergence
        is zero.
        """
        sel = self.providers_table.selectionModel()
        row = sel.selectedRows()[0].row() if sel and sel.selectedRows() else -1
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
        # Available Models — comma-separated text on disk, parsed
        # to a tuple here so the form's snapshot carries the
        # canonical form. The form's ``provider_models_edit``
        # re-renders the tuple back to comma-separated text so the
        # user sees the same view either side.
        models_text = models_item.text() if models_item is not None else ""
        models = self._parse_aliases(models_text)
        is_default = bool(
            name_item.data(Qt.ItemDataRole.UserRole)
            if name_item is not None else False
        )
        self._set_provider_form_enabled(True)
        self.provider_name_edit.blockSignals(True)
        self.provider_url_edit.blockSignals(True)
        self.provider_key_combo.blockSignals(True)
        self.provider_models_edit.blockSignals(True)
        try:
            self.provider_name_edit.setText(name_text)
            self.provider_url_edit.setText(url_text)
            # Repopulate the API Key combo with the *current* keys
            # then select the row's stored ref — combo contents
            # can change between selections (Add/Remove on the API
            # Keys tab), so a stale index would mis-display.
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
        # ``default`` row is non-removable; disable the Remove
        # button so the user doesn't even see it enabled.
        if hasattr(self, "providers_remove_btn"):
            self.providers_remove_btn.setEnabled(not is_default)
        self._refresh_provider_apply_state()

    def _current_provider_models(self, name: str) -> tuple[str, ...]:
        """Return the ``available_models`` tuple for the named provider.

        Reads the column-3 cell text on the providers table and
        parses it back to a tuple via ``_parse_aliases`` (same
        dedupe / trim rules). Returns ``()`` if the provider can't
        be found — the caller can decide whether ``()`` should
        prompt a warning or fall through.

        Used by the Model Configuration tab to populate its
        per-row Model dropdown when the user picks a provider.
        """
        target = name.strip()
        if not target:
            return ()
        for row in range(self.providers_table.rowCount()):
            item = self.providers_table.item(row, 0)
            if item is None or item.text().strip() != target:
                continue
            models_item = self.providers_table.item(row, 3)
            if models_item is None:
                return ()
            return self._parse_aliases(models_item.text())
        return ()

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
        # Snapshot is ``(name, url, key_ref, available_models,
        # is_default)``; the trailing ``is_default`` flag doesn't
        # participate in the divergence check (it's structural).
        return cur != (snapshot[0], snapshot[1], snapshot[2], snapshot[3])

    def _on_provider_apply(self) -> None:
        """Commit the form's values back to the selected provider row.

        Default row's name is locked to ``"default"`` (refused with a
        warning). URL changes on the default row are allowed — the
        default is the fall-through target, not a special immutable
        record. Renames are not separately tracked; ``_on_accept``
        re-reads the providers table directly, so the displayed row
        text is what gets persisted.

        Available Models parses + dedupes via ``_parse_aliases``;
        the canonical comma-joined form is reflected back into both
        the cell and the form so the user sees the normalised
        version they just persisted (mirrors the alias flow on the
        Model Configuration tab).
        """
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
            if name_item is not None else False
        )
        if is_default and new_name != "default":
            QMessageBox.warning(
                self,
                "Default provider name",
                "The first row must be named 'default'. Pick another "
                "name to add a new provider.",
            )
            return
        # Replace the row's text items in place. The Name cell's
        # ``UserRole`` flag (default-ness) is preserved by reading
        # it before the setText.
        if name_item is not None:
            name_item.setText(new_name)
        if url_item is not None:
            url_item.setText(new_url)
        # API Key column: re-render via the shared helper so the
        # display name + yellow stale-ref hint refresh.
        key_item = self._make_provider_key_item(new_key)
        key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.providers_table.setItem(row, 2, key_item)
        # Available Models column: comma-separated text.
        models_item = self.providers_table.item(row, 3)
        if models_item is None:
            models_item = QTableWidgetItem()
            models_item.setFlags(models_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.providers_table.setItem(row, 3, models_item)
        models_item.setText(self._format_aliases(new_models))
        # Reflect the canonical models text back into the form so the
        # user sees the deduped/trimmed version.
        self.provider_models_edit.blockSignals(True)
        try:
            self.provider_models_edit.setText(self._format_aliases(new_models))
        finally:
            self.provider_models_edit.blockSignals(False)
        self._provider_row_snapshot = (
            new_name, new_url, new_key, new_models, is_default,
        )
        # Provider changes invalidate the Model Configuration tab's
        # per-row provider combo AND its model combo (which depends
        # on the provider's available_models). Rebuild both.
        self._refresh_endpoint_provider_combo_for_provider_name(new_name)
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
            # Re-populate the API Key combo with the *current* keys
            # and select the snapshot's ref. The combo's contents
            # can change between selection and revert (Add/Remove
            # on the API Keys tab), so a stale ``setCurrentIndex``
            # by name would leave the combo on a wrong entry.
            self._repopulate_provider_key_combo_for_ref(key_ref)
            self.provider_models_edit.setText(self._format_aliases(models))
        finally:
            self.provider_name_edit.blockSignals(False)
            self.provider_url_edit.blockSignals(False)
            self.provider_key_combo.blockSignals(False)
            self.provider_models_edit.blockSignals(False)
        self._refresh_provider_apply_state()

    def _on_provider_add(self) -> None:
        """Append a new provider row with a placeholder name + empty URL.

        The placeholder name is unique (``provider-N``). The URL is
        blank — the user fills it in via the form. The new row is
        auto-selected so the form mirrors it.
        """
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
        """Delete the selected provider row; refuses for the default row."""
        row = self._selected_provider_row
        if row <= 0:
            return
        name_item = self.providers_table.item(row, 0)
        if name_item is None:
            return
        # Belt-and-braces: only remove if this isn't the default row.
        if name_item.data(Qt.ItemDataRole.UserRole):
            return
        self.providers_table.removeRow(row)
        # The provider list changed → the Model Configuration tab's
        # per-row provider combo needs to drop the deleted provider.
        self._refresh_endpoint_provider_combo()

    def _collect_providers(self) -> dict[str, Provider]:
        """Read the providers table into ``{name: Provider(name, url, key_ref, available_models)}``.

        Skips rows whose name or URL is empty. The ``default``
        provider is always present at row 0 (created in
        ``_populate_providers_table`` if missing); the rest follow
        in their current table order.

        The ``key_ref`` is read from the API Key cell's
        ``UserRole``; ``None`` means no proxy-side auth (the cell
        text reads ``"(no key)"``). The ``available_models`` tuple
        is parsed back from the column-3 cell text via
        ``_parse_aliases`` — same dedupe / trim rules used on
        write.
        """
        out: dict[str, Provider] = {}
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
            out[name] = Provider(
                name=name,
                url=url,
                key_ref=key_ref,
                available_models=available_models,
            )
        return out
