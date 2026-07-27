"""Modal dialog for creating / editing Claude profiles.

Sections (left-to-right in a vertical layout, mirroring
:class:`src.proxy.settings_dialog.ProxySettingsDialog`):

1. **General** — name (the slug is auto-derived), default_model,
   session_cwd path picker. ``default_model`` is validated against
   the keys in the model_endpoints table.
2. **Model Endpoints** — ``QTableWidget(0, 2)`` with Add / Remove.
3. **API Key** — password-mode ``QLineEdit`` plus a ref label.
   A ref like ``"default-key"`` is created automatically on first
   edit if the profile didn't have one.
4. **Environment Variables** — ``QTableWidget(0, 2)`` Add / Remove.
5. **Claude Settings** — raw JSON ``QPlainTextEdit``, validated on OK.
6. **Subagents** — ``QListWidget`` + Add / Edit / Remove. The Edit
   action opens a small sub-dialog (defined at the bottom of this
   file) for the per-subagent fields.

Profile records carry only the credential ``ref``; the actual key is
held by :class:`src.profiles.credential_store.CredentialStore`. The
dialog edits a *copy* of the profile and a copy of the credential —
the store is only updated on OK, mirroring ``ProxySettingsDialog``'s
``exec_and_return()`` pattern.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.credential_store import CredentialStore
from src.profiles.env_var_catalog import (
    StandardEnvVar,
    sections as _catalog_sections,
    standard_env_vars as _standard_env_vars,
)
from src.profiles.models import ClaudeProfile, ModelEndpoint, Provider, Subagent, slugify


# ---- helpers ---------------------------------------------------------------






def _table_pairs(table: QTableWidget) -> list[tuple[str, str]]:
    """Read a 2-column key/value table, dropping rows where either side is blank."""
    out: list[tuple[str, str]] = []
    for row in range(table.rowCount()):
        k_item = table.item(row, 0)
        v_item = table.item(row, 1)
        k = k_item.text().strip() if k_item else ""
        v = v_item.text().strip() if v_item else ""
        if k and v:
            out.append((k, v))
    return out


def _populate_pairs(table: QTableWidget, pairs: list[tuple[str, str]]) -> None:
    table.setRowCount(0)
    for k, v in pairs:
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(k))
        table.setItem(row, 1, QTableWidgetItem(v))


# ---- main dialog -----------------------------------------------------------


class ProfileEditorDialog(QDialog):
    """Edit one :class:`ClaudeProfile` plus its credential value.

    Parameters
    ----------
    profile
        The profile being edited. Pass a fresh instance to create a
        new profile (one with a unique name); pass an existing one
        to edit.
    credential_store
        Where the API key for this profile lives. The dialog calls
        ``set(ref, value)`` on OK if the key field is non-empty.
    parent
        Qt parent widget.
    """

    def __init__(
        self,
        profile: ClaudeProfile,
        *,
        credential_store: CredentialStore,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Claude Profile — {profile.name}")
        self.resize(720, 720)
        # Edit a copy; never mutate the caller's profile in place.
        self._profile = copy.deepcopy(profile)
        self._creds = credential_store
        # Track per-key edits so ``_on_accept`` knows which keys to
        # write to the CredentialStore. Map: ref-name -> True if the
        # user typed a value for that row during this session.
        # ``name -> typed value text`` for keys whose value was
        # edited and Applied. ``name -> True`` (legacy sentinel) is
        # also accepted on a best-effort basis — see ``_on_accept``.
        # ``__deleted__<name> -> True`` tracks rows the user removed
        # from the table so their credentials are deleted on save.
        self._cred_edited_by_key: dict[str, str | bool] = {}
        # Set when the row's *name* was edited (the user renamed a
        # key). Stored so we can delete the old ref on save.
        self._key_renames: dict[str, str] = {}
        # Snapshot of which keys were on disk at open time, so we can
        # detect deletions and remove them on save.
        self._initial_key_names: set[str] = set(self._creds.all_refs().keys())
        # Item-change guard — ``itemChanged`` fires for every setItem
        # we make during _populate, and we don't want that to mark
        # everything as edited. The flag is set during _populate.
        self._populating = False
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- intro / name ------------------------------------------------
        intro = QLabel(
            "Configure a Claude Code session profile. The proxy will route "
            "requests for each model to its configured endpoint. Sessions "
            "always go through the embedded proxy at localhost."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ---- Tabbed body -----------------------------------------------
        # Six tabs in this order: General, Model Endpoints, API Key,
        # Environment Variables, Settings.JSON, Subagents. Each tab
        # is built by a small dedicated helper below; the helpers
        # build the inner widgets once and stash them on ``self``
        # so ``_populate`` and ``_on_accept`` continue to work
        # without modification.
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_endpoints_tab()
        self._build_providers_tab()
        self._build_api_key_tab()
        self._build_env_vars_tab()
        self._build_settings_tab()
        self._build_subagents_tab()

        # ---- OK / Cancel -------------------------------------------------
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    # ---- per-tab builders ------------------------------------------------

    def _make_tab(self, inner: QWidget) -> QWidget:
        """Wrap ``inner`` in a ``QWidget`` with a ``QVBoxLayout`` for the tab.

        Re-parenting into the container keeps the same widget instances
        (and their layouts) — we don't rebuild the form layout, just
        give it a new top-level owner. Margins are a small inset so the
        form doesn't kiss the tab border.
        """
        container = QWidget()
        inner.setParent(container)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        container_layout.addWidget(inner)
        return container

    def _build_general_tab(self) -> None:
        """Tab 1: name, default model, proxy bind/port/config_dir, session CWD.

        The proxy / network fields live here because they're identity-
        adjacent — the bind/port/config_dir determine *which* proxy the
        profile controls, alongside the profile's own name and default
        model. This was the original placement in the linear-flow dialog;
        tabbing just relocates the same fields into one panel rather
        than splitting them across "General" and "Network".
        """
        general_box = QWidget()
        general_layout = QFormLayout(general_box)
        general_layout.setContentsMargins(0, 0, 0, 0)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Default")
        general_layout.addRow("Name:", self.name_edit)

        # Default model — a ``QComboBox`` populated from the alias list,
        # never free-text. Aliases are the user-facing identity of a
        # model (the real upstream IDs like ``MiniMax-M3`` stay
        # hidden in the Model Configuration table); the proxy's
        # alias-rewrite layer translates the chosen alias back to
        # the real name at request time.
        #
        # Items added at populate-time:
        #   - ``(no default)``  — when ``default_model`` is unset
        #     (``userData = None``).
        #   - every deduped alias across all endpoint rows
        #     (``userData = alias_string``).
        #   - ``(real model: X)`` — fallback for legacy profiles whose
        #     ``default_model`` was a real upstream ID; selecting
        #     this preserves the verbatim value through save (the
        #     proxy still accepts a real name if the user prefers).
        self.default_model_combo = QComboBox()
        self.default_model_combo.setEditable(False)
        general_layout.addRow("Default model:", self.default_model_combo)

        # ---- Proxy / network settings ----------------------------------
        # Bind address + port + config_dir moved off ``ProxyConfig``
        # onto the profile — the proxy's network identity is now a
        # per-profile concern, so switching active profile restarts
        # the proxy with new host/port/config-dir.
        self.bind_edit = QLineEdit()
        self.bind_edit.setPlaceholderText("127.0.0.1 (loopback) or 0.0.0.0 (LAN)")
        general_layout.addRow("Proxy bind address:", self.bind_edit)

        self.port_edit = QSpinBox()
        self.port_edit.setRange(0, 65535)
        # ``0`` means "let the OS pick a free port" — the actual bound
        # port is reported via ``ProxyServerThread.started_ok``.
        self.port_edit.setSpecialValueText("auto (OS picks free port)")
        general_layout.addRow("Proxy port:", self.port_edit)

        config_row = QHBoxLayout()
        self.config_dir_edit = QLineEdit()
        self.config_dir_edit.setPlaceholderText("(auto-discover)")
        config_browse = QToolButton()
        config_browse.setText("Browse…")
        config_browse.clicked.connect(self._browse_config_dir)
        config_row.addWidget(self.config_dir_edit, 1)
        config_row.addWidget(config_browse)
        config_container = QWidget()
        config_container.setLayout(config_row)
        general_layout.addRow("Proxy config dir:", config_container)

        cwd_row = QHBoxLayout()
        self.cwd_edit = QLineEdit()
        self.cwd_edit.setPlaceholderText("(inherit GUI process cwd)")
        cwd_browse = QToolButton()
        cwd_browse.setText("Browse…")
        cwd_browse.clicked.connect(self._browse_cwd)
        cwd_row.addWidget(self.cwd_edit, 1)
        cwd_row.addWidget(cwd_browse)
        cwd_container = QWidget()
        cwd_container.setLayout(cwd_row)
        general_layout.addRow("Session CWD:", cwd_container)

        self.tabs.addTab(self._make_tab(general_box), "General")

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

        self.endpoints_table = QTableWidget(0, 3)
        # Aliases-first column order. Aliases are the user-facing
        # identity of the row (the real upstream model ID is the
        # ``Model`` column at the far right). ``UserRole`` on the
        # Provider cell still carries ``provider_name`` as the
        # stable identity; aliases are read back via cell text.
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
        # Highlight the API Key cell with a yellow background when
        # the row's stored ``key_ref`` doesn't match any defined key
        # (stale reference); the table's still read-only, the hint
        # is just visual.
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

    def _build_settings_tab(self) -> None:
        """Tab 5: raw ``settings.json`` editor."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        settings_help = QLabel(
            "Free-form <code>settings.json</code> keys (permissions, hooks, "
            "etc.). <em>model</em> and <em>availableModels</em> are set "
            "automatically from the fields above and override anything "
            "here."
        )
        settings_help.setWordWrap(True)
        settings_help.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(settings_help)

        self.settings_edit = QPlainTextEdit()
        # Instance-method call: ``QPlainTextEdit.font()`` inherited from
        # QWidget isn't bound to the class in PySide6; calling it on the
        # class directly raises ``TypeError: unbound method needs an
        # argument``. Use the widget we just instantiated.
        mono = self.settings_edit.font()
        mono.setFamily(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family())
        self.settings_edit.setFont(mono)
        self.settings_edit.setPlaceholderText('{\n  "permissions": {}\n}')
        # Stretch factor 1 so the editor fills the available space
        # on this tab (which has no other growing content).
        tab_layout.addWidget(self.settings_edit, 1)

        self.tabs.addTab(tab, "Settings.JSON")

    def _build_subagents_tab(self) -> None:
        """Tab 6: ``agents/<name>.md`` subagent editor."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        sub_help = QLabel(
            "Each subagent becomes a <code>&lt;config&gt;/agents/&lt;name&gt;.md</code> "
            "file with YAML frontmatter (Claude Code's native format)."
        )
        sub_help.setWordWrap(True)
        sub_help.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(sub_help)

        sub_row = QHBoxLayout()
        self.subagent_list = QListWidget()
        sub_row.addWidget(self.subagent_list, 1)
        sub_btns = QVBoxLayout()
        sub_add = QPushButton("Add…")
        sub_add.clicked.connect(self._add_subagent)
        sub_edit = QPushButton("Edit…")
        sub_edit.clicked.connect(self._edit_subagent)
        sub_remove = QPushButton("Remove")
        sub_remove.clicked.connect(self._remove_subagent)
        sub_btns.addWidget(sub_add)
        sub_btns.addWidget(sub_edit)
        sub_btns.addWidget(sub_remove)
        sub_btns.addStretch(1)
        sub_btn_container = QWidget()
        sub_btn_container.setLayout(sub_btns)
        sub_row.addWidget(sub_btn_container)
        sub_container = QWidget()
        sub_container.setLayout(sub_row)
        tab_layout.addWidget(sub_container, 1)

        self.tabs.addTab(tab, "Subagents")

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        lbl.setStyleSheet("margin-top: 8px;")
        return lbl

    def _wrap_in_scroll(self, widget: QWidget, *, max_height: int) -> QScrollArea:
        """Wrap a widget in a fixed-height ``QScrollArea``.

        The two key/value tables in this dialog (Model Endpoints,
        Environment Variables) can grow unboundedly — a profile with
        10+ model entries would push the rest of the dialog content
        off-screen. Wrapping each table in a scroll area with a
        ``max_height`` cap means more than ~2 rows are reachable via
        scroll, but the dialog layout itself stays compact.

        ``setWidgetResizable(True)`` is required so the inner widget
        is sized to the scroll area's width; ``setMaximumHeight`` is
        what limits how much vertical real estate the scroll region
        takes before its own scrollbar appears.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        scroll.setMaximumHeight(max_height)
        # Hide the frame so the table blends into the surrounding
        # dialog — without this, a 1px grey border makes the scroll
        # region look like a separate panel.
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

    def _browse_config_dir(self) -> None:
        """Show a directory picker for the proxy's config dir."""
        start = self.config_dir_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Proxy config directory", start
        )
        if chosen:
            self.config_dir_edit.setText(chosen)

    def _populate(self) -> None:
        # Set the guard so the cascade of ``setItem`` calls below
        # don't trip ``_on_key_item_changed`` — those slots should
        # only react to user input, not the initial population.
        # (Endpoints table is read-only now, so no itemsChanged
        # signal needed there.)
        self._populating = True
        try:
            self.name_edit.setText(self._profile.name)
            # Repopulate the default-model dropdown from the
            # alias catalog — ``_populate_default_model_combo``
            # adds the (no default) entry plus every deduped alias
            # across all endpoint rows, and pre-selects the
            # current ``profile.default_model`` if it's still in
            # the list.
            self.default_model_combo.blockSignals(True)
            try:
                self.default_model_combo.clear()
                self._populate_default_model_combo()
            finally:
                self.default_model_combo.blockSignals(False)
            self.cwd_edit.setText(self._profile.session_cwd or "")
            # Proxy / network fields. The spinbox shows the special
            # "auto" text when ``port == 0`` so the user can pick a
            # free port via the OS without typing a magic number.
            self.bind_edit.setText(self._profile.bind_address or "127.0.0.1")
            self.port_edit.setValue(int(self._profile.port or 0))
            self.config_dir_edit.setText(self._profile.config_dir or "")
            # ---- API Keys tab FIRST ----------------------------------
            # The endpoints table's per-row combo lists the keys
            # currently in the keys table. We populate the keys table
            # first so the combos built below have a complete list.
            self._populate_keys_table()
            # ---- Providers tab NEXT ---------------------------------
            # The endpoints tab's per-row Provider combo lists the
            # providers currently in the providers table. We populate
            # the providers table before endpoints so each endpoint
            # can resolve its ``provider_name`` to a URL on open.
            self._populate_providers_table()
            # ---- endpoints tab ---------------------------------------
            # If the on-disk profile has no ``model_endpoints`` but
            # the providers expose catalogs of models, auto-seed one
            # row per ``(provider, model)`` pair so the user doesn't
            # have to remember to repeat each model from the
            # Providers tab in the Model Configuration tab too.
            # Without this seed step, a profile whose Providers tab
            # has a catalog but no Model Configuration rows would
            # surface an empty ``availableModels`` to Claude Code
            # (only built-in / hardcoded defaults show up in
            # ``/model``).
            self._maybe_seed_endpoints_from_provider_catalog()
            self.endpoints_table.setRowCount(0)
            for model_name, endpoint in self._profile.model_endpoints.items():
                self._append_endpoint_row(model_name, endpoint)
            # Ensure at least one row so the user has somewhere to type.
            if self.endpoints_table.rowCount() == 0:
                self._append_endpoint_row("", None)
            _populate_pairs(self.env_table, list(self._profile.env_vars.items()))
            try:
                settings_text = json.dumps(
                    self._profile.claude_settings, indent=2, sort_keys=True
                )
            except (TypeError, ValueError):
                settings_text = ""
            self.settings_edit.setPlainText(settings_text)
            self._refresh_subagent_list()
        finally:
            self._populating = False
        # Auto-select the first row in each table so the editor
        # forms are populated. The form mirrors the selected row;
        # without a selection the form stays disabled.
        if self.keys_table.rowCount() > 0:
            self.keys_table.selectRow(0)
        if self.providers_table.rowCount() > 0:
            self.providers_table.selectRow(0)
        if self.endpoints_table.rowCount() > 0:
            self.endpoints_table.selectRow(0)
        if self.env_table.rowCount() > 0:
            self.env_table.selectRow(0)

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

    def _refresh_endpoint_key_columns(self) -> None:
        """No-op stub kept for backwards compatibility.

        Pre-refactor the API Key column on the Model Configuration
        tab reflected per-endpoint ``key_ref`` references into the
        global ``CredentialStore``. After the refactor, keys live
        on the *provider* — the endpoints table no longer carries
        a key column. Any stale callers (e.g. ``_on_key_add`` /
        ``_on_key_remove`` invoke this after the keys table changes)
        become no-ops; the model combo on the endpoint form already
        rebuilds itself when the underlying providers table changes
        via ``_refresh_endpoint_model_combo``.
        """
        return None

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

    def _refresh_subagent_list(self) -> None:
        self.subagent_list.clear()
        for sa in self._profile.subagents:
            item = QListWidgetItem(sa.name)
            item.setData(Qt.ItemDataRole.UserRole, sa)
            self.subagent_list.addItem(item)

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

    def _browse_cwd(self) -> None:
        start = self.cwd_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Session working directory", start)
        if chosen:
            self.cwd_edit.setText(chosen)

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

    # ---- Model Endpoints tab slots ------------------------------------

    def _on_endpoint_add(self) -> None:
        """Append a blank endpoint row and select it."""
        self._append_endpoint_row("", None)
        # Auto-select the new row so the user can immediately type
        # into the editor form below.
        new_row = self.endpoints_table.rowCount() - 1
        self.endpoints_table.selectRow(new_row)

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
        """Fill the General tab's ``default_model_combo`` dropdown.

        Items, in display order:
          1. ``(no default)`` — sentinel; ``userData`` is ``None``.
             Picked when ``profile.default_model`` is unset.
          2. Every deduped alias across all endpoint rows, in
             sorted order. ``userData`` is the alias string.
             These are the *preferred* choices — what Claude Code
             surfaces in ``/model``.
          3. ``(real model: X)`` fallback — when the profile has a
             ``default_model`` value that's a real upstream ID
             (e.g. a legacy profile from before this redesign),
             surface it as a labeled entry so the user's existing
             choice still round-trips through save verbatim. The
             dropdown's ``currentData()`` reads back the real name;
             the proxy still accepts a real model name on the
             wire (it skips the alias-rewrite layer for known
             names).

        Pre-selection: the current ``profile.default_model`` if
        present in the list, otherwise index 0 (``(no default)``).
        """
        # Sorted + deduped alias catalog across every endpoint.
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
        # Legacy fallback: ``default_model`` set to a real name
        # but no matching alias. Render it as a labeled last-resort
        # entry so save doesn't silently drop the user's choice.
        legacy_real = self._profile.default_model
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
        # Pre-select the matching entry.
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
    # ---- Providers tab: row selection + form editor --------------------

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

    # ---- Environment Variables tab: row selection + form editor -------

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

    def _add_subagent(self) -> None:
        name, ok = QInputDialog.getText(self, "New Subagent", "Name:")
        if not ok or not name.strip():
            return
        new_sa = Subagent(name=name.strip(), description="", system_prompt="")
        if self._edit_subagent_in_place(new_sa):
            self._profile.subagents.append(new_sa)
            self._refresh_subagent_list()

    def _edit_subagent(self) -> None:
        item = self.subagent_list.currentItem()
        if item is None:
            return
        sa = item.data(Qt.ItemDataRole.UserRole)
        if self._edit_subagent_in_place(sa):
            item.setText(sa.name)
            item.setData(Qt.ItemDataRole.UserRole, sa)

    def _remove_subagent(self) -> None:
        item = self.subagent_list.currentItem()
        if item is None:
            return
        sa = item.data(Qt.ItemDataRole.UserRole)
        self._profile.subagents = [s for s in self._profile.subagents if s is not sa]
        self._refresh_subagent_list()

    def _edit_subagent_in_place(self, sa: Subagent) -> bool:
        dlg = _SubagentEditorDialog(sa, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            updated = dlg.to_subagent()
            sa.name = updated.name
            sa.description = updated.description
            sa.system_prompt = updated.system_prompt
            sa.tools = updated.tools
            sa.model = updated.model
            return True
        return False

    def _on_accept(self) -> None:
        # ---- snap-apply pending form edits ---------------------------
        # The form-below-editor pattern requires a separate Apply
        # click to commit. If the user types in the form and clicks OK
        # without Apply, their typed values are read from the table
        # cells (not the form) — which are still the row's old
        # values, and the typed text is silently lost. Snap-apply
        # any divergent form state just before reading the table so
        # the user's last-typed text is always saved. Disabled when
        # there's no selection or no divergence.
        if (
            getattr(self, "_selected_endpoint_row", -1) >= 0
            and self._endpoint_form_values_diverge_from_row()
        ):
            self._on_endpoint_apply()
        if (
            getattr(self, "_selected_provider_row", -1) >= 0
            and self._provider_form_values_diverge_from_row()
        ):
            self._on_provider_apply()
        if (
            getattr(self, "_selected_key_row", -1) >= 0
            and self._key_form_values_diverge_from_row()
        ):
            self._on_key_apply()
        if (
            getattr(self, "_selected_env_row", -1) >= 0
            and self._env_form_values_diverge_from_row()
        ):
            self._on_env_apply()

        # ---- validate name ---------------------------------------------
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Profile name cannot be empty.")
            return
        self._profile.name = name
        self._profile.slug = slugify(name)

        # ---- API Keys: validate uniqueness + persist -------------------
        key_names = self._current_key_names()
        if "default" not in key_names:
            QMessageBox.warning(
                self,
                "Missing default key",
                "The 'default' API key cannot be removed. "
                "Re-add it before saving.",
            )
            return
        seen: set[str] = set()
        dups: list[str] = []
        for n in key_names:
            if n in seen:
                dups.append(n)
            seen.add(n)
        if dups:
            QMessageBox.warning(
                self,
                "Duplicate API key names",
                f"Each API key must have a unique name. Duplicates: {dups!r}.",
            )
            return
        # Process deletions first — renames look up the *current*
        # store state, and a delete + insert in quick succession
        # needs the delete to clear the old ref before the rename
        # picks up the new one.
        for ref_name in list(self._cred_edited_by_key.keys()):
            if ref_name.startswith("__deleted__"):
                deleted_name = ref_name[len("__deleted__"):]
                try:
                    self._creds.delete(deleted_name)
                except Exception:
                    pass
        # Apply renames: copy the old key's value into the new
        # name, then delete the old ref.
        for new_name, old_name in self._key_renames.items():
            try:
                existing = self._creds.get(old_name)
                if existing is not None:
                    self._creds.set(new_name, existing)
                self._creds.delete(old_name)
            except Exception:
                pass
        # Apply value edits. ``_cred_edited_by_key`` now stores the
        # *value text* the user typed (not just True), so we can
        # apply it directly without re-reading the form.
        for ref_name, value in list(self._cred_edited_by_key.items()):
            if ref_name.startswith("__deleted__"):
                continue
            if not isinstance(value, str):
                continue
            if value == "":
                continue
            try:
                self._creds.set(ref_name, value)
            except Exception:
                pass

        # ---- model_endpoints -------------------------------------------
        # Persist providers first so endpoint validation can confirm
        # every endpoint's ``provider_name`` still exists.
        providers = self._collect_providers()
        if "default" not in providers:
            QMessageBox.warning(
                self,
                "Missing default provider",
                "A 'default' provider is required. Re-add it on the "
                "Providers tab before saving.",
            )
            return
        # Validate provider-name uniqueness — two providers with the
        # same name would silently alias each other.
        seen_prov: set[str] = set()
        dups_prov: list[str] = []
        for n in providers:
            if n in seen_prov:
                dups_prov.append(n)
            seen_prov.add(n)
        if dups_prov:
            QMessageBox.warning(
                self,
                "Duplicate provider names",
                f"Each provider must have a unique name. Duplicates: {dups_prov!r}.",
            )
            return
        self._profile.providers = providers

        endpoints = self._collect_endpoints()
        if not endpoints:
            QMessageBox.warning(
                self,
                "No model endpoints",
                "Add at least one model endpoint so the proxy knows where to route.",
            )
            return
        # Warn about endpoints whose provider_name doesn't match any
        # defined provider. The form's combo falls back to
        # ``(no provider)`` on select if the provider is missing,
        # but the user might have ignored that and clicked OK
        # anyway.
        stale_providers: list[str] = []
        for model, ep in endpoints.items():
            if ep.provider_name not in providers:
                stale_providers.append(f"{model} -> {ep.provider_name}")
        if stale_providers:
            QMessageBox.warning(
                self,
                "Missing provider references",
                "These model/endpoint rows reference a provider that "
                "no longer exists. The row's Provider dropdown has "
                "been reset to '(no provider)' — re-add the provider "
                "on the Providers tab or pick another provider, then "
                "save again.\n\n"
                f"Rows: {stale_providers!r}",
            )
            return
        # Each endpoint's model must be in its provider's
        # ``available_models`` catalog. The form's Model combo is
        # constrained at build time, but the user could have left
        # a stale model in a cell before Apply (e.g. they edited
        # the provider's catalog and an old model isn't there
        # anymore), or migrated from a profile written under the
        # old "free-text model" shape. Refuse to save and surface
        # every offender.
        stale_models: list[str] = []
        for model, ep in endpoints.items():
            provider = providers.get(ep.provider_name)
            if provider is None:
                continue
            catalog = tuple(provider.available_models or ())
            if catalog and model not in catalog:
                stale_models.append(
                    f"{model!r} not in {ep.provider_name!r}.available_models"
                )
        if stale_models:
            QMessageBox.warning(
                self,
                "Model not in provider catalog",
                "Each model must appear in the catalog of the "
                "provider that hosts it. Add the model to the "
                "provider's Available Models field on the "
                "Providers tab, or pick a different provider.\n\n"
                f"Models: {stale_models!r}",
            )
            return
        # Aliases — validate uniqueness across the profile. Two real
        # models claiming the same alias would silently shadow each
        # other at request time, so refuse to save and let the user
        # pick one. Alias-vs-real-name collisions (an alias that
        # matches a different real model) are flagged as a soft
        # warning so the user can confirm the intent.
        alias_owner: dict[str, str] = {}
        # ``alias_conflicts`` is a dict of ``alias -> [all models
        # claiming it]``. Walking the dict in stable insertion order
        # is enough for the warning message — we don't care which
        # model "won" the first claim, only that multiple models
        # claim the same alias.
        alias_conflicts: dict[str, list[str]] = {}
        for model, ep in endpoints.items():
            for alias in ep.aliases:
                if not isinstance(alias, str) or not alias:
                    continue
                if alias in alias_owner:
                    if alias not in alias_conflicts:
                        alias_conflicts[alias] = [alias_owner[alias]]
                    alias_conflicts[alias].append(model)
                else:
                    alias_owner[alias] = model
        if alias_conflicts:
            pretty_parts: list[str] = []
            for a, models in alias_conflicts.items():
                claimed_by = ", ".join(repr(m) for m in models)
                pretty_parts.append(f"{a!r} (claimed by {claimed_by})")
            pretty = ", ".join(pretty_parts)
            QMessageBox.warning(
                self,
                "Duplicate model aliases",
                "Each alias must be claimed by exactly one model. "
                "Two or more models list the same alias — pick "
                "one to keep it on, or rename the others.\n\n"
                f"Conflicts: {pretty}",
            )
            return
        # Soft warning when an alias collides with a real model name.
        alias_real_collisions: list[str] = []
        for model in endpoints:
            for alias in endpoints[model].aliases:
                if alias in endpoints and alias != model:
                    alias_real_collisions.append(
                        f"{alias!r} (alias of {model!r}) shadows real model"
                    )
        if alias_real_collisions:
            answer = QMessageBox.warning(
                self,
                "Alias shadows a real model",
                "One or more aliases have the same name as a different "
                "real model in this profile. Requests for that name will "
                "route to the alias's target, not the real model — that's "
                "probably what you want, but worth confirming.\n\n"
                f"Collisions: {alias_real_collisions!r}\n\n"
                "Save anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._profile.model_endpoints = endpoints

        # ---- default_model ----------------------------------------
        # The General tab's dropdown is populated from aliases
        # (preferred) plus a ``(real model: X)`` fallback for
        # legacy profiles. Both choices are routable: an alias
        # flows through the proxy's alias-rewrite layer at request
        # time; a real model name resolves directly through
        # ``model_endpoints`` lookup. We accept either; ``(no
        # default)`` → ``None`` clears the field.
        default_model: str | None = self.default_model_combo.currentData()
        if default_model is not None:
            default_model = str(default_model).strip() or None
        self._profile.default_model = default_model

        # ---- session_cwd ------------------------------------------------
        cwd = self.cwd_edit.text().strip() or None
        self._profile.session_cwd = cwd

        # ---- proxy / network --------------------------------------------
        # ``bind_address`` falls back to loopback if blank — we don't
        # want the user to accidentally expose the proxy on the LAN
        # by clearing the field. ``port`` of 0 means "OS picks a free
        # port" (set by the spinbox's special-value-text).
        bind = self.bind_edit.text().strip() or "127.0.0.1"
        self._profile.bind_address = bind
        self._profile.port = int(self.port_edit.value())
        self._profile.config_dir = self.config_dir_edit.text().strip()
        # ``is_default`` is structural — the editor doesn't allow the
        # user to toggle it, so preserve whatever's on the in-memory
        # profile copy.

        # ---- env_vars ---------------------------------------------------
        self._profile.env_vars = dict(_table_pairs(self.env_table))

        # ---- claude_settings (raw JSON) ---------------------------------
        raw = self.settings_edit.toPlainText().strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                QMessageBox.warning(
                    self,
                    "Invalid JSON",
                    f"Claude Settings is not valid JSON: {e}",
                )
                return
            if not isinstance(parsed, dict):
                QMessageBox.warning(
                    self,
                    "Invalid JSON",
                    "Claude Settings must be a JSON object (not a list or scalar).",
                )
                return
            self._profile.claude_settings = parsed
        else:
            self._profile.claude_settings = {}

        self.accept()

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

    # ---- public API --------------------------------------------------------

    def to_profile(self) -> ClaudeProfile:
        """Return the edited profile (caller's responsibility to store it)."""
        return self._profile


# ---- env vars picker -------------------------------------------------------


class StandardEnvVarsDialog(QDialog):
    """Pick one or more curated Claude Code env vars.

    Rendered as a two-column ``QListWidget`` — variable name on the
    left, purpose on the right — sectioned by category (Model
    Selection, Timeouts & Network, etc.). Multi-select via ctrl/cmd
    click or shift-click; ``ExtendedSelection`` covers both.

    The picker is read-only: it doesn't edit values, only *adds*
    variable names to the parent dialog's env table. Selected
    variables land in the parent table without values; the user
    fills in values afterwards, the same workflow as ``Add Custom``.

    Variables already present in the parent table are still
    selectable but show a "(already added)" hint so the user can
    tell. Selecting them again would just re-add a blank row —
    harmless, since :func:`_table_pairs` drops blank-value rows on
    save.
    """

    def __init__(
        self,
        *,
        already_added: frozenset[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Standard Environment Variables")
        self.resize(640, 480)
        # Catalog + already-added set captured at construction; the
        # picker is a snapshot, not a live view.
        self._already_added: frozenset[str] = already_added or frozenset()
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        help_label = QLabel(
            "Pick one or more Claude Code / Anthropic environment variables to "
            "add to this profile. They will be added with no value set — fill "
            "in the value on the profile editor after picking. <b>Ctrl/Cmd+click</b> "
            "to select multiple, or <b>Shift+click</b> for a range."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(help_label)

        # Two-column list. The variable name is the primary text;
        # the purpose is rendered as the right column via a custom
        # delegate would be heavier than necessary — instead we use
        # ``setItemWidget``-style rows by leveraging ``QListWidgetItem``
        # with a tool-tip carrying the purpose, plus a one-line prefix
        # to indicate the section. The simpler design — single column
        # with purpose as tooltip — is rejected because the user asked
        # for "two columns, one for variable and one for purpose, just
        # like on the web docs page".
        #
        # The lightest Qt-native way to get two columns in a list is
        # ``QTableView`` with ``QStandardItemModel``. We use that
        # instead, with read-only items and section headers rendered
        # as bold rows via a custom delegate-free trick: insert a
        # non-selectable header row at the start of each section.
        from PySide6.QtGui import QStandardItem, QStandardItemModel
        from PySide6.QtWidgets import QTableView

        self.table = QTableView()
        self.model = QStandardItemModel(0, 2, self)
        self.model.setHorizontalHeaderLabels(["Variable", "Purpose"])
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        # The whole dialog already has its own scroll behaviour; a
        # fixed maximum height here keeps long lists usable without
        # dominating the screen.
        self.table.setMinimumHeight(280)
        layout.addWidget(self.table, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _populate(self) -> None:
        """Fill the model with section headers + variable rows.

        Section headers are inserted as non-selectable, non-editable
        bold rows whose background blends with the dialog. Each
        variable is two cells: name (left) and purpose (right).
        Variables already on the parent profile get a ``*`` suffix
        on the name column so the user knows re-adding is harmless
        but redundant.
        """
        from PySide6.QtGui import QBrush, QColor, QStandardItem
        from PySide6.QtCore import Qt as _Qt

        header_brush = QBrush(QColor("#dde4ec"))
        header_flags = (
            _Qt.ItemFlag.ItemIsEnabled
        )  # NOT selectable
        for section in _catalog_sections():
            name_item = QStandardItem(section)
            name_item.setFlags(header_flags)
            name_item.setBackground(header_brush)
            # Span the row so the header reads as one wide label.
            name_item.setData("header", _Qt.ItemDataRole.UserRole)
            purpose_item = QStandardItem("")
            purpose_item.setFlags(header_flags)
            purpose_item.setBackground(header_brush)
            self.model.appendRow([name_item, purpose_item])
            for var in _variables_in_section(section):
                left_text = var.name
                if var.name in self._already_added:
                    left_text = f"{var.name}  *"
                left = QStandardItem(left_text)
                right = QStandardItem(var.purpose)
                # Both cells selectable; clicking the row highlights
                # both because ``SelectRows`` is on.
                self.model.appendRow([left, right])

    def selected_names(self) -> list[str]:
        """Return the variable names the user picked, in display order.

        Section headers are skipped — they aren't selectable anyway,
        but we double-check by ignoring items whose ``UserRole`` is
        ``"header"``. ``*`` suffix (the "already added" marker) is
        stripped before returning.
        """
        from PySide6.QtCore import Qt as _Qt

        out: list[str] = []
        for index in self.table.selectionModel().selectedRows():
            name_item = self.model.item(index.row(), 0)
            if name_item is None:
                continue
            if name_item.data(_Qt.ItemDataRole.UserRole) == "header":
                continue
            raw = name_item.text().strip()
            # Strip the "already added" marker if present.
            if raw.endswith("*"):
                raw = raw[: -len("  *")]
            if raw:
                out.append(raw)
        return out


def _variables_in_section(section: str) -> list[StandardEnvVar]:
    """Subset of :func:`standard_env_vars` whose section matches."""
    return [v for v in _standard_env_vars() if v.section == section]


# ---- sub-dialog ------------------------------------------------------------


class _SubagentEditorDialog(QDialog):
    """Tiny editor for one :class:`Subagent`."""

    def __init__(self, subagent: Subagent, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Subagent — {subagent.name or 'New'}")
        self.resize(560, 480)
        self._sa = copy.deepcopy(subagent)
        layout = QFormLayout(self)

        self.name_edit = QLineEdit(self._sa.name)
        layout.addRow("Name:", self.name_edit)

        self.desc_edit = QLineEdit(self._sa.description)
        layout.addRow("Description:", self.desc_edit)

        self.tools_edit = QLineEdit(", ".join(self._sa.tools or []))
        self.tools_edit.setPlaceholderText("comma-separated, e.g. Read, Edit, Bash")
        layout.addRow("Tools:", self.tools_edit)

        self.model_edit = QLineEdit(self._sa.model or "")
        self.model_edit.setPlaceholderText("(inherit session default)")
        layout.addRow("Model:", self.model_edit)

        self.prompt_edit = QPlainTextEdit(self._sa.system_prompt)
        self.prompt_edit.setPlaceholderText("System prompt body…")
        layout.addRow(self.prompt_edit)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addRow(self.button_box)

    def to_subagent(self) -> Subagent:
        name = self.name_edit.text().strip()
        tools_raw = self.tools_edit.text().strip()
        tools = (
            [t.strip() for t in tools_raw.split(",") if t.strip()]
            if tools_raw
            else None
        )
        model = self.model_edit.text().strip() or None
        return Subagent(
            name=name,
            description=self.desc_edit.text(),
            system_prompt=self.prompt_edit.toPlainText(),
            tools=tools,
            model=model,
        )


# ---- manage profiles list dialog -----------------------------------------


class ManageProfilesDialog(QDialog):
    """List-and-act dialog for the user's profile collection.

    The user said:

        "When going to Manage Profiles, a list of the current
        profiles should be presented, with the option to
        add/remove/edit them. Clicking edit would then open the
        existing Manage Profiles dialogue window, where API key,
        models etc can be set. There is always a default profile
        at the top of the list that cannot be deleted, but it can
        be edited and then those changes saved. When creating a
        new profile, the current settings of the default profile
        are used as the base."

    Concretely the dialog is a ``QListWidget`` of every profile
    (with the Default one pinned at index 0) plus three buttons:

    * **Add New** — opens :class:`ProfileEditorDialog` with a fresh
      profile seeded from the Default profile's fields. On OK the
      new profile is upserted via the store.
    * **Edit** — opens :class:`ProfileEditorDialog` for the selected
      profile. Available for *every* profile including Default.
    * **Delete** — removes the selected profile. Disabled when the
      selection is the Default profile.

    The dialog is intentionally *not* a wizard — it stays open
    between edits so the user can manage multiple profiles in one
    session without re-opening it from the menu each time.
    """

    def __init__(
        self,
        profile_store,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Lazy import keeps the import graph of ``src.profiles`` free
        # of Qt — the store itself is Qt-free.
        from src.profiles.store import (
            DEFAULT_PROFILE_NAME,
            ProfileStore,
        )

        self._store: ProfileStore = profile_store
        self._default_name = DEFAULT_PROFILE_NAME
        self.setWindowTitle("Manage Profiles")
        self.resize(480, 360)
        self._build_ui()
        self._refresh_list()
        # No selection by default; the Edit / Delete buttons start
        # disabled and enable on the first selection.

    # ---- UI ------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        help_label = QLabel(
            "Profiles bundle the proxy's model endpoints with the "
            "Claude Code session settings (env vars, settings.json, "
            "subagents). The Default profile is the always-on template; "
            "it can't be deleted but can be edited."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(help_label)

        self.list_widget = QListWidget()
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        # Double-click is a fast path to Edit — the same pattern
        # every native file manager uses.
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._on_edit())
        layout.addWidget(self.list_widget, 1)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("Add New…")
        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn = QPushButton("Edit…")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        layout.addWidget(btn_container)

        # Close button — no other state to commit; the dialog is a
        # view over the on-disk store, so every action already
        # persisted when its slot returned.
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        close_container = QWidget()
        close_container.setLayout(close_row)
        layout.addWidget(close_container)

    # ---- list helpers --------------------------------------------------

    def _refresh_list(self) -> None:
        """Reload the list from the store.

        ``load()`` always returns Default at index 0, so the visual
        order matches the contract. We annotate the Default row
        with ``"(default)"`` so the user can tell it apart from
        user-created profiles at a glance.
        """
        self.list_widget.clear()
        for p in self._store.load():
            label = p.name
            if getattr(p, "is_default", False) or p.name == self._default_name:
                label = f"{p.name}  (default)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.list_widget.addItem(item)
        # Keep selection invariant: if there was a selection, restore
        # it (the user might be in the middle of editing this
        # profile and the refresh is from an Add / Edit).
        if self._last_selected_name is not None:
            for i in range(self.list_widget.count()):
                it = self.list_widget.item(i)
                if it.data(Qt.ItemDataRole.UserRole).name == self._last_selected_name:
                    self.list_widget.setCurrentRow(i)
                    break
        self._on_selection_changed()

    @property
    def _last_selected_name(self) -> str | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        p = item.data(Qt.ItemDataRole.UserRole)
        return p.name if p is not None else None

    def _on_selection_changed(self) -> None:
        item = self.list_widget.currentItem()
        has_sel = item is not None
        self.edit_btn.setEnabled(has_sel)
        # Default is non-deletable. The button is enabled for any
        # other profile — the store's ``delete`` also refuses the
        # Default as a belt-and-braces guard.
        is_default = False
        if has_sel:
            p = item.data(Qt.ItemDataRole.UserRole)
            is_default = (
                p is not None
                and (getattr(p, "is_default", False) or p.name == self._default_name)
            )
        self.delete_btn.setEnabled(has_sel and not is_default)

    # ---- actions -------------------------------------------------------

    def _on_add(self) -> None:
        """Open the editor with a fresh profile seeded from Default.

        The user said: "When creating a new profile, the current
        settings of the default profile are used as the base." We
        start from ``ProfileStore.ensure_default()`` (the on-disk
        Default) so the new profile inherits the live model
        endpoints, env vars, and bind/port/config_dir.
        """
        default = self._store.ensure_default()
        # ``copy.deepcopy`` is required — the editor mutates the
        # profile in place, and we don't want the Default to drift
        # if the user cancels the editor.
        draft = copy.deepcopy(default)
        draft.name = ""
        draft.slug = ""  # auto-derived from name in _on_accept
        draft.is_default = False  # it's a new profile, not Default
        self._open_editor_for(draft, after_save=lambda new_p: self._on_added(new_p))

    def _on_edit(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        p = item.data(Qt.ItemDataRole.UserRole)
        if p is None:
            return
        # Edit is allowed for every profile, including Default. The
        # editor handles the name change by re-deriving the slug.
        self._open_editor_for(
            copy.deepcopy(p),
            after_save=lambda new_p: self._on_edited(new_p),
        )

    def _open_editor_for(
        self,
        draft,
        *,
        after_save,
    ) -> None:
        """Run the editor; on accept, save via the store and refresh.

        ``after_save`` is called with the freshly-saved profile so
        the caller can decide what post-save UI side effects to run
        (re-activate the profile, restart the proxy, etc.).
        """
        dlg = ProfileEditorDialog(
            draft,
            credential_store=self._store.credential_store,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        edited = dlg.to_profile()
        if not edited.name.strip():
            return
        # If we're editing the Default, force ``is_default=True`` so
        # the flag survives the round-trip.
        if edited.name == self._default_name:
            edited.is_default = True
        self._store.upsert(edited)
        after_save(edited)
        self._refresh_list()

    def _on_added(self, new_profile) -> None:
        """Post-save hook for Add New.

        The new profile is left inactive (Default stays active) so
        the user opts in to the new routing via the menu. This
        matches the seeded-from-default semantics: a new profile is
        a starting point, not an auto-activation. The caller is
        expected to wire a proxy restart through the existing
        :meth:`MainWindow._show_proxy_settings` flow if the active
        profile's endpoints were edited via the editor.
        """
        # Nothing to do at the dialog level; refresh already happened
        # in _open_editor_for. This hook exists so MainWindow can
        # subclass behaviour if needed.
        pass

    def _on_edited(self, edited_profile) -> None:
        """Post-save hook for Edit.

        If the edited profile is the active one, the caller (the
        main window) is responsible for restarting the proxy so
        the new bind/port/config_dir/model_endpoints take effect.
        """
        pass

    def _on_delete(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        p = item.data(Qt.ItemDataRole.UserRole)
        if p is None:
            return
        # Belt-and-braces — the button is disabled for Default, but
        # also guard here.
        if p.name == self._default_name or getattr(p, "is_default", False):
            return
        # Confirm before deletion — removing a profile deletes its
        # ``claude_profiles/<slug>/`` directory via the next
        # ``write_profile_config`` cycle, which is destructive.
        confirm = QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile {p.name!r}? This can't be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self._store.delete(p.name)
        self._refresh_list()