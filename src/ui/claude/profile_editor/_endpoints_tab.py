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
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.models import ModelEndpoint


class EndpointsTabMixin:
    def _build_endpoints_tab(self) -> None:
        """Tab 2: model → (endpoint URL, API key) mapping.

        The table is *read-only* — clicking a row populates the
        editor form below, where the user types a new value and
        clicks **Apply**. This avoids Qt's in-place cell editor
        entirely (the previous round of cell-editor + cell-widget
        races segfaulted PySide6). Apply is disabled until the
        form's values diverge from the selected row.

        Columns:
        - **Model** — model name (``claude-opus-4-8`` etc.)
        - **Endpoint URL** — upstream URL the proxy routes to
        - **API Key** — key the combo below maps to (display only;
          edited via the form's ``QComboBox``)
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        ep_help = QLabel(
            "Select a row to edit it in the form below. The table "
            "is read-only — the form's <b>Apply</b> button commits "
            "your typed values to the row. Pressing <b>OK</b> also "
            "commits any pending form edits automatically, so a "
            "forgotten Apply never costs you data. Use <b>Add…</b> "
            "for new rows and <b>Remove Selected</b> to delete. "
            "Aliases you add here are exposed as Claude Code's "
            "<em>availableModels</em> — pick one with "
            "<code>/model</code> to use it. The proxy's alias "
            "rewrite translates the alias back to the real model "
            "name at request time."
        )
        ep_help.setWordWrap(True)
        ep_help.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(ep_help)

        # Default Model — moved from the old General tab. Stored
        # under ``claude_settings["model"]`` on save (the
        # per-profile ``settings.json`` writes the same key Claude
        # Code reads). The combo is populated by
        # ``_populate_default_model_combo`` which the dialog calls
        # from its own ``_populate`` step.
        from PySide6.QtWidgets import QFormLayout

        default_box = QWidget()
        default_layout = QFormLayout(default_box)
        default_layout.setContentsMargins(0, 0, 0, 0)
        self.default_model_combo = QComboBox()
        self.default_model_combo.setEditable(False)
        default_layout.addRow("Default model:", self.default_model_combo)
        tab_layout.addWidget(default_box)

        self.endpoints_table = QTableWidget(0, 3)
        # Column order: Aliases | Provider | Model.
        # The profile editor only sees the *Provider*; the
        # provider's ``key_ref`` (set in Claude > Settings >
        # Providers) carries the implicit auth — the profile
        # editor deliberately has no API Key column so users
        # don't edit credentials per-profile. API key values
        # live in Claude > Settings > API Keys; the per-provider
        # ``key_ref`` in Claude > Settings > Providers picks
        # which one to use at request time.
        self.endpoints_table.setHorizontalHeaderLabels(
            ["Aliases", "Provider", "Model"]
        )
        self.endpoints_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.endpoints_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.endpoints_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.endpoints_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # Read-only: no in-place editing of any column. The form
        # below is the editor — this is what avoids the segfault.
        self.endpoints_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # No API Key column — auth lives on the provider's
        # ``key_ref`` (set in Claude > Settings > Providers).
        # Stale-ref highlighting isn't surfaced in the profile
        # editor; the proxy launcher surfaces missing keys via
        # the ``ANTHROPIC_API_KEY`` sentinel.
        self.endpoints_table.itemSelectionChanged.connect(
            self._on_endpoint_selection_changed
        )
        # Wrap in a QScrollArea so the table region has its own
        # vertical scrollbar — without this, a profile with more
        # than ~2 endpoints pushes the rest of the tab off-screen.
        self.endpoints_table.setMinimumHeight(96)
        self.endpoints_table_scroll = self._wrap_in_scroll(self.endpoints_table, max_height=180)
        tab_layout.addWidget(self.endpoints_table_scroll)

        ep_btns = QHBoxLayout()
        ep_add = QPushButton("Add…")
        ep_add.clicked.connect(self._on_endpoint_add)
        ep_remove = QPushButton("Remove Selected")
        ep_remove.clicked.connect(
            lambda: self._remove_selected_rows(self.endpoints_table)
        )
        ep_btns.addWidget(ep_add)
        ep_btns.addWidget(ep_remove)
        ep_btns.addStretch(1)
        ep_btn_container = QWidget()
        ep_btn_container.setLayout(ep_btns)
        tab_layout.addWidget(ep_btn_container)

        # ---- Selected row editor -------------------------------------
        # Three ``QLineEdit`` + ``QComboBox`` form fields; ``Apply``
        # is disabled until the form's values diverge from the
        # selected row's stored values. Lives below the Add/Remove
        # buttons so the workflow is: select a row → edit the
        # form → click Apply.
        self._build_endpoints_editor_form(tab_layout)

        self.tabs.addTab(tab, "Model Configuration")

    def _build_endpoints_editor_form(
        self, parent_layout: QVBoxLayout
    ) -> None:
        """Build the read-only-table + form-below editor for endpoints.

        The form is grouped under a horizontal rule (a 1px ``QLabel``
        styled via ``setFrameShape``) so it's visually distinct from
        the table region above. ``Apply`` is enabled only when
        ``_endpoints_form_values_diverge_from_row`` is True.
        """
        from PySide6.QtWidgets import (
            QFormLayout,
            QGroupBox,
            QLineEdit,
            QPushButton,
        )

        # Index of the row whose data the form currently mirrors.
        # ``-1`` means "no row selected". When the user types into
        # the form, the index is unchanged — only the form values
        # drift, and we re-check ``_endpoints_form_values_diverge``
        # to enable/disable Apply.
        self._selected_endpoint_row: int = -1
        # Snapshot of the row's values at form-load time, so we
        # can detect divergence quickly without reparsing the
        # ``QTableWidgetItem``. ``None`` means "no row selected".
        # Post-refactor tuple is ``(model, provider_name, aliases)``
        # — the URL and auth come from the named provider at
        # write time, so neither lives on the endpoint row.
        self._endpoint_row_snapshot: (
            tuple[str, str, tuple[str, ...]] | None
        ) = None

        editor_box = QGroupBox("Edit selected row")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        # The Model field is now a ``QComboBox``, not a free-text
        # ``QLineEdit``. Items are populated from the selected
        # provider's ``available_models``; switching the Provider
        # below triggers a model combo rebuild via
        # ``_refresh_endpoint_model_combo``. ``userData`` carries
        # the model name (same as the display text — kept for
        # symmetry with the other combos).
        self.endpoint_model_combo = QComboBox()
        editor_layout.addRow("Model:", self.endpoint_model_combo)

        # The endpoint's URL is no longer free-text — it's a
        # ``QComboBox`` populated from the Providers tab. The
        # display label is the provider's *name*; the ``userData``
        # carries the URL (resolved at combo-rebuild time) so the
        # URL hint label below can show the resolved URL.
        self.endpoint_provider_combo = QComboBox()
        # ``(no provider)`` is the empty / pre-addition state.
        self.endpoint_provider_combo.addItem("(no provider)", "")
        editor_layout.addRow("Provider:", self.endpoint_provider_combo)

        # Inline hint showing the URL of the currently-selected
        # provider — users want to see the URL they're picking
        # even though they're picking by name.
        self.endpoint_url_hint = QLabel("")
        self.endpoint_url_hint.setStyleSheet("color: gray; font-size: 11px;")
        self.endpoint_url_hint.setWordWrap(True)
        editor_layout.addRow("Provider URL:", self.endpoint_url_hint)

        # Aliases — short names clients can use in place of the real
        # model name. The proxy rewrites ``req.model = alias`` to
        # the real name before routing. Comma-separated in the
        # form; persisted as a tuple on ``ModelEndpoint.aliases``.
        self.endpoint_aliases_edit = QLineEdit()
        self.endpoint_aliases_edit.setPlaceholderText(
            "comma-separated, e.g. opus, team-pro"
        )
        editor_layout.addRow("Aliases:", self.endpoint_aliases_edit)

        # ``editingFinished`` fires on F2/Enter/blur; ``textChanged``
        # / ``currentIndexChanged`` fire continuously — the latter
        # drive the Apply enable logic in real time.
        self.endpoint_model_combo.currentIndexChanged.connect(
            self._on_endpoint_form_changed
        )
        self.endpoint_provider_combo.currentIndexChanged.connect(
            self._on_endpoint_form_changed
        )
        # Update the URL hint + repopulate the model combo on
        # every provider selection change.
        self.endpoint_provider_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_endpoint_url_hint()
        )
        self.endpoint_provider_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_endpoint_model_combo()
        )
        self.endpoint_aliases_edit.textChanged.connect(
            self._on_endpoint_form_changed
        )

        # ``Apply`` + ``Revert``. ``Apply`` is enabled *only* when
        # the form's values diverge from the selected row's stored
        # values; ``Revert`` always returns the form to that
        # baseline (and disables Apply until the user re-edits).
        btn_row = QHBoxLayout()
        self.endpoint_apply_btn = QPushButton("Apply")
        self.endpoint_apply_btn.setEnabled(False)
        self.endpoint_apply_btn.clicked.connect(self._on_endpoint_apply)
        self.endpoint_revert_btn = QPushButton("Revert")
        self.endpoint_revert_btn.setEnabled(False)
        self.endpoint_revert_btn.clicked.connect(self._on_endpoint_revert)
        btn_row.addStretch(1)
        btn_row.addWidget(self.endpoint_apply_btn)
        btn_row.addWidget(self.endpoint_revert_btn)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        editor_layout.addRow("", btn_container)

        # Status hint — "no row selected" / "apply to enable" /
        # "ready to apply" depending on state.
        self.endpoint_form_status = QLabel("Select a row to edit")
        self.endpoint_form_status.setStyleSheet("color: gray; font-size: 11px;")
        editor_layout.addRow("", self.endpoint_form_status)

        # All form widgets disabled until a row is selected.
        self._set_endpoint_form_enabled(False)

        parent_layout.addWidget(editor_box)

    def _refresh_endpoint_provider_combo(self) -> None:
        """Rebuild the Model Configuration form's provider ``QComboBox``.

        Items are: every defined provider. The display label is the
        provider's *name*; the ``userData`` is the URL (so the form
        can write back the resolved URL on Apply).

        Re-selects the previously-selected provider by *name* if it's
        still in the list; otherwise falls back to ``"(no provider)"``
        if available, or the first item. The form's divergence check
        runs at the end so Apply enable/disable stays correct.

        Called when the providers table changes (Add / Remove /
        Rename). Mirrors ``_repopulate_endpoint_key_combo`` for the
        API-Key column on the same form.
        """
        if not hasattr(self, "endpoint_provider_combo"):
            # The form hasn't been built yet (called during early
            # population). Bail; the next refresh from
            # ``_on_endpoint_selection_changed`` will rebuild.
            return
        previous_name = self.endpoint_provider_combo.currentData()
        self.endpoint_provider_combo.blockSignals(True)
        try:
            self.endpoint_provider_combo.clear()
            names = self._current_provider_names()
            for name in names:
                url = self._current_provider_url(name) or ""
                self.endpoint_provider_combo.addItem(name, url)
            # Re-select prior provider by name; fall back to first
            # provider if the previous selection was deleted.
            target_idx = -1
            for i in range(self.endpoint_provider_combo.count()):
                if names[i] == previous_name:
                    target_idx = i
                    break
            if target_idx < 0 and self.endpoint_provider_combo.count() > 0:
                target_idx = 0
            if target_idx >= 0:
                self.endpoint_provider_combo.setCurrentIndex(target_idx)
        finally:
            self.endpoint_provider_combo.blockSignals(False)
        self._refresh_endpoint_apply_state()

    def _refresh_endpoint_provider_combo_for_provider_name(
        self, _name: str
    ) -> None:
        """Rebuild the Model Configuration form's combo + restore selection.

        Same as ``_refresh_endpoint_provider_combo`` but the prior
        selection comes from the *row*, not the combo's currentData
        (the row's provider_name is the source of truth).
        """
        self._refresh_endpoint_provider_combo()

    @staticmethod
    def _parse_aliases(text: str) -> tuple[str, ...]:
        """Parse the comma-separated aliases field into a deduped tuple.

        ``"opus, team-pro, opus"`` → ``("opus", "team-pro")``. Empty
        strings (from trailing commas or whitespace runs) are dropped.
        Order is preserved (left-to-right after dedup).
        """
        if not text:
            return ()
        parts = [s.strip() for s in text.split(",")]
        return tuple(dict.fromkeys(p for p in parts if p))

    @staticmethod
    def _format_aliases(aliases: tuple[str, ...]) -> str:
        """Inverse of ``_parse_aliases``: tuple → comma-separated string.

        ``("opus", "team-pro")`` → ``"opus, team-pro"``. Empty tuple
        → ``""``.
        """
        return ", ".join(aliases) if aliases else ""

    def _append_endpoint_row(
        self,
        model_name: str,
        endpoint: ModelEndpoint | None,
    ) -> None:
        """Insert one read-only row into the endpoints table.

        Three-column table: Aliases | Provider | Model. Aliases
        are first (the user-facing identity of the row); the real
        upstream model ID is the last column. ``UserRole`` on the
        Provider cell carries the stable ``provider_name``.

        Brand-new rows (no endpoint) default the provider to the
        first defined provider (almost always ``"default"``). Legacy
        rows with a missing ``provider_name`` leave the cell empty;
        the form falls back to ``"(no provider)"`` so the user is
        forced to make a choice.
        """
        row = self.endpoints_table.rowCount()
        self.endpoints_table.insertRow(row)
        # Aliases column — comma-separated text. The canonical
        # tuple lives on ``ModelEndpoint.aliases``; the cell's text
        # is parsed back into a tuple on read
        # (``_collect_endpoints``).
        aliases = endpoint.aliases if endpoint is not None else ()
        aliases_item = QTableWidgetItem(self._format_aliases(aliases))
        aliases_item.setFlags(aliases_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.endpoints_table.setItem(row, 0, aliases_item)
        provider_name = endpoint.provider_name if endpoint is not None else ""
        if not provider_name:
            providers = self._current_provider_names()
            provider_name = providers[0] if providers else ""
        provider_item = QTableWidgetItem(provider_name)
        provider_item.setFlags(provider_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        # ``UserRole`` carries the stable provider_name. The URL
        # is rendered as a hint line under the form's Provider
        # field, since the URL belongs to the provider and can
        # change without touching this row.
        provider_item.setData(Qt.ItemDataRole.UserRole, provider_name)
        self.endpoints_table.setItem(row, 1, provider_item)
        # Model column — the real upstream ID (hidden by the
        # Aliases column upstream of it).
        model_item = QTableWidgetItem(model_name)
        model_item.setFlags(model_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.endpoints_table.setItem(row, 2, model_item)
        # No API Key column — auth lives on the provider's
        # ``key_ref`` (set in Claude > Settings > Providers);
        # the profile editor doesn't surface credentials.

    def _on_endpoint_add(self) -> None:
        """Append a blank endpoint row and select it."""
        self._append_endpoint_row("", None)
        # Auto-select the new row so the user can immediately type
        # into the editor form below.
        new_row = self.endpoints_table.rowCount() - 1
        self.endpoints_table.selectRow(new_row)

    def _current_provider_names(self) -> list[str]:
        """Snapshot of provider names from the global settings store.

        Pre-restructure this read from a per-profile ``providers``
        table on the deleted Providers tab; now we read from the
        global store. The form's provider combo and the auto-seed
        helper both consume this.
        """
        names: list[str] = []
        settings = self._global_settings_for_validation()
        providers = settings.providers or {}
        for name in providers.keys():
            if isinstance(name, str) and name not in names:
                names.append(name)
        return names

    def _current_provider_url(self, name: str) -> str | None:
        """URL of the named provider in the global registry, or ``None``."""
        settings = self._global_settings_for_validation()
        prov = (settings.providers or {}).get(name)
        if prov is None:
            return None
        if isinstance(prov, dict):
            url = str(prov.get("url") or "")
        else:
            url = str(getattr(prov, "url", "") or "")
        return url or None

    def _current_provider_models(self, name: str) -> tuple[str, ...]:
        """``available_models`` tuple for the named provider."""
        settings = self._global_settings_for_validation()
        prov = (settings.providers or {}).get(name)
        if prov is None:
            return ()
        if isinstance(prov, dict):
            raw = prov.get("available_models") or ()
            if isinstance(raw, (list, tuple)):
                return tuple(
                    dict.fromkeys(
                        str(m).strip() for m in raw if str(m).strip()
                    )
                )
            if isinstance(raw, str):
                return tuple(
                    dict.fromkeys(s.strip() for s in raw.split(",") if s.strip())
                )
            return ()
        return tuple(getattr(prov, "available_models", ()) or ())

    def _global_settings_for_validation(self):
        """Return the current :class:`GlobalSettings`.

        Resolution order:
        1. ``self._global_settings_loader`` — installed by the
           production main window (calls into the live
           :class:`GlobalSettingsStore`).
        2. ``self._global_settings_store`` — passed as a kwarg
           to the dialog (test fixtures).
        3. On-disk default at ``data_dir()/claude_global.json``.

        Returns the dataclass value either way.
        """
        from src.profiles.global_settings import GlobalSettingsStore
        from src.proxy.paths import data_dir

        loader = getattr(self, "_global_settings_loader", None)
        if loader is not None:
            try:
                settings = loader()
                if settings is not None:
                    return settings
            except (OSError, ValueError):
                pass
        store = getattr(self, "_global_settings_store", None)
        if store is not None:
            try:
                return store.load()
            except (OSError, ValueError):
                pass
        return GlobalSettingsStore(data_dir() / "claude_global.json").load()

    # ---- Endpoints tab: row selection + form editor -----------------------

    def _on_endpoint_selection_changed(self) -> None:
        """Mirror the currently-selected row's data into the editor form.

        Triggered by ``itemSelectionChanged``. If multiple rows are
        selected we use the first one; if no row is selected we
        disable the form. Apply is reset to disabled on every
        selection change (the form is freshly populated so it
        matches the row, hence not "divergent").

        Three columns: Aliases (col 0), Provider (col 1),
        Model (col 2). The Model field is a ``QComboBox`` whose
        items are rebuilt from the selected provider's
        ``available_models`` once the Provider combo settles.
        """
        sel = self.endpoints_table.selectionModel()
        if sel is None:
            row = -1
        else:
            rows = sel.selectedRows()
            row = rows[0].row() if rows else -1
        self._selected_endpoint_row = row
        if row < 0:
            self._endpoint_row_snapshot = None
            self._set_endpoint_form_enabled(False)
            self.endpoint_form_status.setText("Select a row to edit")
            self._refresh_endpoint_apply_state()
            return
        # Pull the row's values into the form. Columns are now
        # reordered: Aliases first, Provider, Model.
        aliases_item = self.endpoints_table.item(row, 0)
        provider_item = self.endpoints_table.item(row, 1)
        model_item = self.endpoints_table.item(row, 2)
        # Aliases — read from col 0's text (parsed back to a
        # tuple). Empty text → empty tuple.
        aliases = self._parse_aliases(
            aliases_item.text() if aliases_item is not None else ""
        )
        # The Provider cell's ``UserRole`` carries the *provider_name*
        # (the stable identity).
        provider_name = ""
        if provider_item is not None:
            stored = provider_item.data(Qt.ItemDataRole.UserRole)
            if isinstance(stored, str):
                provider_name = stored
        # Model — read from col 2's text. If the cell is empty
        # (e.g. the user just typed in the form without clicking
        # Apply), the form's model combo will fall back to its
        # first valid item, which is fine.
        model_text = model_item.text() if model_item is not None else ""
        self._set_endpoint_form_enabled(True)
        # Block signals so we don't run the divergence check
        # mid-population.
        self.endpoint_model_combo.blockSignals(True)
        self.endpoint_provider_combo.blockSignals(True)
        self.endpoint_aliases_edit.blockSignals(True)
        try:
            # Rebuild the provider combo so it's in sync with the
            # current providers table — then pick the provider by
            # its stable *name*.
            self._repopulate_endpoint_provider_combo_for_name_and_url(
                provider_name, ""
            )
            # Model combo — populated from the *current* selected
            # provider's ``available_models``. Keep the previous
            # model selected if it's still in the list.
            self._refresh_endpoint_model_combo(model_text)
            # Aliases — display as comma-separated text in the form.
            self.endpoint_aliases_edit.setText(self._format_aliases(aliases))
            # Update the URL hint with the resolved URL.
            self._refresh_endpoint_url_hint()
        finally:
            self.endpoint_model_combo.blockSignals(False)
            self.endpoint_provider_combo.blockSignals(False)
            self.endpoint_aliases_edit.blockSignals(False)
        # Snapshot form values so divergence is straightforward to
        # detect on each ``_on_endpoint_form_changed`` tick.
        selected_provider_name = (
            self.endpoint_provider_combo.currentText()
            if self.endpoint_provider_combo.currentIndex() >= 0
            else ""
        )
        selected_model = (
            self.endpoint_model_combo.currentText()
            if self.endpoint_model_combo.currentIndex() >= 0
            else ""
        )
        self._endpoint_row_snapshot = (
            model_text, selected_provider_name, aliases
        )
        self.endpoint_form_status.setText(
            f"Editing row {row + 1} of {self.endpoints_table.rowCount()}"
        )
        self._refresh_endpoint_apply_state()

    def _refresh_endpoint_model_combo(
        self, prefer_model: str = ""
    ) -> None:
        """Rebuild the Model ``QComboBox`` from the selected provider's catalog.

        Items: ``(no model)`` + the provider's ``available_models``
        tuple. ``(no model)`` is the fallback entry used when the
        provider has no catalog (e.g. ``"(no provider)"`` is
        selected, or a brand-new provider the user hasn't populated
        yet) AND when the row's stored model is empty (so a new
        row's snapshot doesn't accidentally pin itself to the first
        catalog item and trigger spurious divergence).

        Selection: ``prefer_model`` if it appears in the fresh
        list; otherwise index 0 (``(no model)``) — *not* the first
        catalog item. This is the bit that makes Apply correctly
        detect a divergent state on a new row: by defaulting to
        ``(no model)`` the snapshot matches the empty cell, so the
        user picking a real model becomes a divergence event.
        """
        if not hasattr(self, "endpoint_model_combo"):
            return
        self.endpoint_model_combo.blockSignals(True)
        try:
            self.endpoint_model_combo.clear()
            self.endpoint_model_combo.addItem("(no model)", "")
            provider_name = self.endpoint_provider_combo.currentText()
            models = self._current_provider_models(provider_name)
            for m in models:
                self.endpoint_model_combo.addItem(m, m)
            target_idx = 0  # default to "(no model)"
            if prefer_model:
                for i in range(self.endpoint_model_combo.count()):
                    if self.endpoint_model_combo.itemText(i) == prefer_model:
                        target_idx = i
                        break
            self.endpoint_model_combo.setCurrentIndex(target_idx)
        finally:
            self.endpoint_model_combo.blockSignals(False)

    def _repopulate_endpoint_provider_combo_for_name_and_url(
        self, name: str, url: str
    ) -> None:
        """Populate the provider combo and select the named provider.

        Selection precedence: the provider whose *name* matches
        ``name`` is preferred (stable identity). If no provider
        matches the name, fall back to matching by URL — this
        handles legacy rows whose ``UserRole`` was empty (older
        profiles that pre-date the providers field).

        ``(no provider)`` (userData ``""``) is the fallback when
        neither name nor URL matches any provider — either because
        the row is brand-new or because the provider that used to
        back this endpoint was deleted.
        """
        self.endpoint_provider_combo.blockSignals(True)
        try:
            self.endpoint_provider_combo.clear()
            self.endpoint_provider_combo.addItem("(no provider)", "")
            target_idx = 0  # default to "(no provider)"
            for prov_name in self._current_provider_names():
                prov_url = self._current_provider_url(prov_name) or ""
                self.endpoint_provider_combo.addItem(prov_name, prov_url)
                if prov_name == name and target_idx == 0:
                    target_idx = self.endpoint_provider_combo.count() - 1
            # Fall back to URL match if name didn't hit (legacy rows).
            if target_idx == 0 and url:
                for i in range(self.endpoint_provider_combo.count()):
                    if self.endpoint_provider_combo.itemData(i) == url:
                        target_idx = i
                        break
            self.endpoint_provider_combo.setCurrentIndex(target_idx)
        finally:
            self.endpoint_provider_combo.blockSignals(False)

    def _refresh_endpoint_url_hint(self) -> None:
        """Render the URL of the currently-selected provider as a hint label.

        Lets the user see *which* URL they're picking without
        scrolling back to the Providers tab.
        """
        if not hasattr(self, "endpoint_url_hint"):
            return
        url = self.endpoint_provider_combo.currentData() or ""
        if not url:
            self.endpoint_url_hint.setText("(no provider selected)")
        else:
            self.endpoint_url_hint.setText(url)

    def _populate_default_model_combo(self) -> None:
        """Fill the Model Configuration tab's ``default_model_combo``.

        Items, in display order:
          1. ``(no default)`` — sentinel; ``userData`` is ``None``.
             Picked when no default model is configured.
          2. Every deduped alias across all endpoint rows, in
             sorted order. ``userData`` is the alias string.
             These are the *preferred* choices — what Claude Code
             surfaces in ``/model``.
          3. ``(real model: X)`` fallback — when the profile has a
             ``model`` value that's a real upstream ID (legacy),
             surface it as a labeled entry so the existing choice
             still round-trips through save verbatim.

        Pre-selection: the current ``claude_settings["model"]`` if
        present in the list, otherwise index 0 (``(no default)``).

        Default model moved from a first-class dataclass field to
        ``claude_settings["model"]`` (the key Claude Code reads in
        ``settings.json``).
        """
        all_aliases: list[str] = []
        for endpoint in self._profile.model_endpoints.values():
            if endpoint is None:
                continue
            for alias in (getattr(endpoint, "aliases", ()) or ()):
                if isinstance(alias, str) and alias.strip():
                    all_aliases.append(alias)
        self.default_model_combo.addItem("(no default)", None)
        for alias in sorted(dict.fromkeys(all_aliases)):
            self.default_model_combo.addItem(alias, alias)
        settings = getattr(self._profile, "claude_settings", {}) or {}
        legacy_real = (
            settings.get("model") if isinstance(settings, dict) else None
        )
        if (
            legacy_real
            and not any(
                self.default_model_combo.itemData(i) == legacy_real
                for i in range(self.default_model_combo.count())
            )
        ):
            self.default_model_combo.addItem(
                f"(real model: {legacy_real})", legacy_real
            )
        target_idx = 0
        if legacy_real:
            for i in range(self.default_model_combo.count()):
                if self.default_model_combo.itemData(i) == legacy_real:
                    target_idx = i
                    break
        if self.default_model_combo.count() > 0:
            self.default_model_combo.setCurrentIndex(target_idx)

    def _repopulate_endpoint_key_combo_for_ref(self, ref: str | None) -> None:
        """Dead-code stub kept for any leftover callers in test mocks.

        Pre-refactor this helper populated a per-endpoint API Key
        ``QComboBox``. After the refactor, authentication lives on
        the provider — ``endpoint_key_combo`` no longer exists on
        this dialog. The endpoint Model Configuration tab now uses
        ``_refresh_endpoint_key_columns`` / ``_refresh_endpoint_model_combo``
        for the equivalent refresh flow. This stub ensures any
        historical ``self._repopulate_endpoint_key_combo_for_ref(...)``
        call sites are no-ops rather than raising ``AttributeError``.
        """
        return None

    def _set_endpoint_form_enabled(self, enabled: bool) -> None:
        """Enable / disable every widget in the editor form.

        Called when the selection changes (no row → disabled, some
        row → enabled). Apply/Revert are managed by
        ``_refresh_endpoint_apply_state`` and reflect the divergence
        check, not the selection.
        """
        self.endpoint_model_combo.setEnabled(enabled)
        self.endpoint_provider_combo.setEnabled(enabled)
        self.endpoint_aliases_edit.setEnabled(enabled)
        if not enabled:
            self.endpoint_apply_btn.setEnabled(False)
            self.endpoint_revert_btn.setEnabled(False)

    def _on_endpoint_form_changed(self, *_args: object) -> None:
        """Re-check whether the form has diverged from the row's values.

        Connected to all three form widgets' change signals. We
        treat "divergent" as "any of the three form values differs
        from the row's snapshot". When divergent, Apply becomes
        enabled; when back to identical, Revert disables Apply.
        """
        if getattr(self, "_selected_endpoint_row", -1) < 0:
            return
        self._refresh_endpoint_apply_state()

    def _refresh_endpoint_apply_state(self) -> None:
        """Toggle the Apply/Revert enabled state based on form divergence."""
        enabled_divergence = self._endpoint_form_values_diverge_from_row()
        self.endpoint_apply_btn.setEnabled(enabled_divergence)
        self.endpoint_revert_btn.setEnabled(enabled_divergence)
        if enabled_divergence:
            self.endpoint_form_status.setText(
                "Edited — click Apply to save, or Revert to discard"
            )
        elif self._selected_endpoint_row >= 0:
            row_label = (
                f"Row {self._selected_endpoint_row + 1}"
                if self._selected_endpoint_row is not None
                else "Selected row"
            )
            self.endpoint_form_status.setText(
                f"{row_label} — no pending changes"
            )

    def _endpoint_form_values_diverge_from_row(self) -> bool:
        """Compare the form's values to the selected row's snapshot.

        Post-refactor tuple is ``(model, provider_name, aliases)`` —
        key_ref + URL are *not* form values anymore (they live on
        the provider, not the endpoint). Returns True when any of
        those three differs from the snapshot.

        Model values are normalised so ``"(no model)"`` (the combo
        sentinel displayed when the cell is empty) compares equal
        to ``""`` (the literal cell text on an empty row). This
        way a freshly-created row reads as non-divergent.
        """
        if self._selected_endpoint_row < 0:
            return False
        snapshot = self._endpoint_row_snapshot
        if snapshot is None:
            return False
        cur_model = self.endpoint_model_combo.currentText().strip()
        if cur_model == "(no model)":
            cur_model = ""
        cur = (
            cur_model,
            self.endpoint_provider_combo.currentText().strip(),
            self._parse_aliases(self.endpoint_aliases_edit.text()),
        )
        return cur != snapshot

    def _on_endpoint_apply(self) -> None:
        """Commit the form's current values back to the selected row.

        Writes the model name (from the Model combo), the
        provider_name (from the Provider combo), and the parsed
        aliases tuple to the row's three cells.

        Re-snapshots the row so subsequent form edits re-derive
        from the new baseline. Apply is disabled until the form
        drifts again.

        Validation lives in ``_on_accept`` — the model dropdown is
        already constrained to the selected provider's catalog at
        build-time, but the dialog rejects any Apply whose model
        has somehow drifted (e.g. the provider's catalog changed
        in another window before save).
        """
        row = self._selected_endpoint_row
        if row < 0:
            return
        if not self._endpoint_form_values_diverge_from_row():
            return
        new_model = self.endpoint_model_combo.currentText().strip()
        # ``(no model)`` sentinel → empty cell text. Pre-refactor
        # we had a free-text field so ``""`` was already valid;
        # after the refactor we constrain to a dropdown, so the
        # sentinel maps to the equivalent on-disk value.
        if new_model == "(no model)":
            new_model = ""
        new_provider_name = self.endpoint_provider_combo.currentText().strip()
        # Pull URL from the Provider combo's UserRole for the cell
        # display — the actual source of truth is the provider
        # name; the URL is rendered for human readability.
        new_provider_url = (
            self.endpoint_provider_combo.currentData() or ""
        )
        if not new_model:
            QMessageBox.warning(
                self,
                "Missing model",
                "Model is required. Pick a model from the dropdown "
                "(the list comes from the selected provider's "
                "Available Models).",
            )
            return
        if (
            not new_provider_name
            or new_provider_name == "(no provider)"
            or new_provider_url == ""
        ):
            QMessageBox.warning(
                self,
                "Missing provider",
                "Pick a Provider for this endpoint. Add one on the "
                "Providers tab if none exist.",
            )
            return
        # Replace the row's QTableWidgetItems in-place (preserving
        # the read-only flag and any styling). New column order:
        # Aliases | Provider | Model.
        new_aliases = self._parse_aliases(self.endpoint_aliases_edit.text())
        aliases_item = self.endpoints_table.item(row, 0)
        if aliases_item is None:
            aliases_item = QTableWidgetItem()
            aliases_item.setFlags(
                aliases_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            self.endpoints_table.setItem(row, 0, aliases_item)
        aliases_item.setText(self._format_aliases(new_aliases))
        provider_item = self.endpoints_table.item(row, 1)
        if provider_item is None:
            provider_item = QTableWidgetItem()
            provider_item.setFlags(provider_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.endpoints_table.setItem(row, 1, provider_item)
        provider_item.setText(new_provider_name)
        # ``UserRole`` carries the provider_name — the stable
        # identity used by ``_collect_endpoints`` on save.
        provider_item.setData(Qt.ItemDataRole.UserRole, new_provider_name)
        # Replace the Model cell (col 2 — the real upstream ID).
        model_item = self.endpoints_table.item(row, 2)
        if model_item is None:
            model_item = QTableWidgetItem()
            model_item.setFlags(model_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.endpoints_table.setItem(row, 2, model_item)
        model_item.setText(new_model)
        # Reflect the canonical aliases text back into the form so the user
        # sees the deduped/trimmed version they just persisted.
        self.endpoint_aliases_edit.blockSignals(True)
        try:
            self.endpoint_aliases_edit.setText(
                self._format_aliases(new_aliases)
            )
        finally:
            self.endpoint_aliases_edit.blockSignals(False)
        # Refresh the baseline so the form is once again non-divergent.
        self._endpoint_row_snapshot = (
            new_model, new_provider_name, new_aliases
        )
        self._refresh_endpoint_apply_state()

    def _on_endpoint_revert(self) -> None:
        """Reset the form to match the selected row's stored values."""
        snapshot = self._endpoint_row_snapshot
        if snapshot is None:
            return
        model_text, provider_name, aliases = snapshot
        self.endpoint_model_combo.blockSignals(True)
        self.endpoint_provider_combo.blockSignals(True)
        self.endpoint_aliases_edit.blockSignals(True)
        try:
            # Re-select the Provider combo by name; then re-populate
            # the Model combo from that provider's catalog and pick
            # ``model_text`` if it's still present.
            self._repopulate_endpoint_provider_combo_for_name_and_url(
                provider_name, ""
            )
            self._refresh_endpoint_model_combo(model_text)
            # Restore aliases text.
            self.endpoint_aliases_edit.setText(self._format_aliases(aliases))
            self._refresh_endpoint_url_hint()
        finally:
            self.endpoint_model_combo.blockSignals(False)
            self.endpoint_provider_combo.blockSignals(False)
            self.endpoint_aliases_edit.blockSignals(False)
        self._refresh_endpoint_apply_state()

    def _collect_endpoints(self) -> dict[str, ModelEndpoint]:
        """Read the endpoints table into ``{model: ModelEndpoint(provider_name, aliases)}``.

        Column order: Aliases (col 0), Provider (col 1), Model
        (col 2). ``key_ref`` and URL no longer live on the
        endpoint — they belong to the provider — so the dataclass
        is constructed from just two fields.

        Skips rows whose model name is empty or whose provider_name
        is empty (a row with no provider is filtered out — Apply
        rejects the row before the user reaches ``_on_accept`` so
        this should be defensive only).

        The provider_name is read from the Provider cell's
        ``UserRole`` (set in ``_append_endpoint_row``) — that's
        the stable identity endpoints carry; the cell's display
        text *is* the provider name (the URL belongs to the
        provider, not the endpoint).

        Aliases come from column 0's text, parsed back into a
        tuple. Empty / malformed text → empty tuple.
        """
        out: dict[str, ModelEndpoint] = {}
        for row in range(self.endpoints_table.rowCount()):
            aliases_item = self.endpoints_table.item(row, 0)
            provider_item = self.endpoints_table.item(row, 1)
            model_item = self.endpoints_table.item(row, 2)
            model = (model_item.text().strip() if model_item else "")
            if not model:
                continue
            provider_name = ""
            if provider_item is not None:
                stored = provider_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(stored, str):
                    provider_name = stored
            if not provider_name:
                continue
            aliases = self._parse_aliases(
                aliases_item.text() if aliases_item is not None else ""
            )
            out[model] = ModelEndpoint(
                provider_name=provider_name,
                aliases=aliases,
            )
        return out
