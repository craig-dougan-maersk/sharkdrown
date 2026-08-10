"""ProfileEditorDialog — thin shell composing per-tab mixins.

The dialog itself owns:
- __init__ (state setup, deepcopy of profile)
- _build_ui (tab widget creation, delegates to mixin _build_*_tab methods)
- _populate (coordinates population order across tabs)
- _on_accept (validation + credential persistence + profile commit)
- to_profile (public API)

All tab-specific build and slot methods live in the mixin modules.
"""

from __future__ import annotations

import copy
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.credential_store import CredentialStore
from src.profiles.models import ClaudeProfile, ModelEndpoint, Provider, slugify

from src.ui.claude.profile_editor._endpoints_tab import EndpointsTabMixin
from src.ui.claude.profile_editor._env_vars_tab import EnvVarsTabMixin
from src.ui.claude.profile_editor._settings_subagents_tab import SettingsSubagentsTabMixin
from src.ui.claude.profile_editor._plugins_tab import PluginsTabMixin
from src.ui.claude.profile_editor._mcp_servers_tab import McpServersTabMixin
from src.ui.claude.profile_editor._container_tab import ContainerTabMixin


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


class ProfileEditorDialog(
    EndpointsTabMixin,
    EnvVarsTabMixin,
    SettingsSubagentsTabMixin,
    PluginsTabMixin,
    McpServersTabMixin,
    ContainerTabMixin,
    QDialog,
):
    """Edit one :class:`ClaudeProfile` plus its credential value.

    Parameters
    ----------
    profile
        The profile being edited. Pass a fresh instance to create a
        new profile; pass an existing one to edit.
    credential_store
        Where the API key for this profile lives.
    parent
        Qt parent widget.
    """

    def __init__(
        self,
        profile: ClaudeProfile,
        *,
        credential_store: CredentialStore,
        global_settings_store: "GlobalSettingsStore | None" = None,
        global_settings_loader: "Callable[[], GlobalSettings] | None" = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Claude Profile — {profile.name}")
        self.resize(720, 720)
        self._profile = copy.deepcopy(profile)
        self._creds = credential_store
        # Stash the credential store on every tab mixin so the
        # Model Configuration tab's read-only "API Key" column
        # can resolve the row's provider's ``key_ref`` against the
        # global credential registry (the same store the Claude
        # > Settings > API Keys tab edits).
        self._credential_store = credential_store
        # Optional GlobalSettingsStore injection — lets tests
        # point the dialog at a fixture store instead of the
        # production ``data_dir()`` location. The production app
        # never passes this kwarg; it relies on the loader to
        # read from ``data_dir()`` indirectly.
        self._global_settings_store = global_settings_store
        # Optional loader (preferred over ``global_settings_store``
        # in production) — installed by the main window so the
        # dialog sees live global state without the user having
        # to commit Claude > Settings first. Falls back to the
        # injected store, then the on-disk default.
        self._global_settings_loader = global_settings_loader
        self._cred_edited_by_key: dict[str, str | bool] = {}
        self._key_renames: dict[str, str] = {}
        self._initial_key_names: set[str] = set(self._creds.all_refs().keys())
        self._populating = False
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Configure a Claude Code session profile. Providers, API keys, "
            "proxy bind/port and the global CLAUDE.md are managed at "
            "Claude → Settings; everything here is per-profile."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # The profile name is the only "general" field that stayed
        # per-profile — it's the identity of the record. Bind /
        # port / config_dir / session CWD moved to Claude > Settings.
        name_row = QHBoxLayout()
        name_label = QLabel("Profile name:")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Default")
        name_row.addWidget(name_label)
        name_row.addWidget(self.name_edit, 1)
        name_container = QWidget()
        name_container.setLayout(name_row)
        layout.addWidget(name_container)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        self._build_endpoints_tab()
        self._build_env_vars_tab()
        self._build_plugins_tab()
        self._build_mcp_servers_tab()
        self._build_settings_tab()
        self._build_subagents_tab()
        self._build_container_tab()

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    # ---- per-tab builders (delegated to mixins via _make_tab) ----

    def _make_tab(self, inner: QWidget) -> QWidget:
        container = QWidget()
        inner.setParent(container)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        container_layout.addWidget(inner)
        return container

    def _wrap_in_scroll(
        self, widget: QWidget, *, max_height: int
    ) -> "QScrollArea":
        """Wrap ``widget`` in a fixed-height scroll area.

        Used by per-tab builders to cap the vertical real estate a
        table / tree view can claim before its own scrollbar
        appears. Extracted into a small wrapper so the moved
        ``GeneralTabMixin`` (which lived here before the menu
        restructure) doesn't drag this helper with it.
        """
        from src.ui.claude._qt_helpers import wrap_in_scroll

        return wrap_in_scroll(widget, max_height=max_height)

    def _remove_selected_rows(self, table: QTableWidget) -> None:
        """Remove the selected rows from ``table`` and reset the form.

        Shared by the Endpoints and Env Vars tabs (both have a
        "Remove Selected" button). The endpoints and env tabs each
        track their selected row on a different attribute — both
        tabs share one ``_selected_*_row`` integer plus a
        ``_on_*_selection_changed`` slot that mirrors the table
        selection into the form. To clear the form after a delete,
        we look up the right resetter by table identity.

        Iteration: ``QTableWidget.removeRow`` re-indexes every row
        below the removed one, so we delete in *reverse* order — a
        forward pass would shift the row indices as we go and skip
        every other row. After the deletion, the form's snapshot
        (``_endpoint_row_snapshot`` / ``_env_row_snapshot``) is
        stale (the row it pointed at may now be a different row,
        or the table may have fewer rows), so we clear it and let
        the table's selection-changed signal repopulate.

        Edge case: removing the last row leaves the table empty
        rather than automatically seeding a blank one. That's
        intentional — the API Keys / Providers tab's "default
        row pinned" pattern doesn't apply here, and the user can
        always click Add to get a fresh blank row.
        """
        # The tabs that use this helper all expose ``_selected_X_row``
        # and a ``_on_X_selection_changed`` slot. Build a tiny
        # mapping from table-widget identity to those symbols so we
        # can dispatch the right reset after the deletion. The
        # ``getattr`` is defensive — a tab that hasn't wired the
        # selection signal yet still gets a clean removal.
        table_to_reset = {
            self.endpoints_table: "_selected_endpoint_row",
            self.env_table: "_selected_env_row",
        }
        sel_model = table.selectionModel()
        if sel_model is None:
            return
        # ``selectedRows`` returns the index column for each row in
        # selection order; sort descending so deletion doesn't
        # re-index past us.
        rows = sorted(
            (idx.row() for idx in sel_model.selectedRows()),
            reverse=True,
        )
        if not rows:
            return
        for row in rows:
            if row < 0 or row >= table.rowCount():
                continue
            table.removeRow(row)
        # Reset the per-tab selection tracker and clear any open
        # form snapshot. The selection-changed signal will fire
        # (with no rows selected) and update the form to
        # "Select a row to edit" — but we set the tracker first
        # so the on-change handler sees ``-1`` and resets the
        # snapshot.
        attr = table_to_reset.get(table)
        if attr is not None:
            setattr(self, attr, -1)
        if table is self.endpoints_table:
            self._endpoint_row_snapshot = None
            self._set_endpoint_form_enabled(False)
            self.endpoint_form_status.setText("Select a row to edit")
            self._refresh_endpoint_apply_state()
        elif table is self.env_table:
            self._env_row_snapshot = None
            self._set_env_form_enabled(False)
            self.env_form_status.setText("Select a row to edit")
            self._refresh_env_apply_state()

    # ---- population (coordinates order across tabs) ----

    def _populate(self) -> None:
        self._populating = True
        try:
            self.name_edit.setText(self._profile.name)
            self.default_model_combo.blockSignals(True)
            try:
                self.default_model_combo.clear()
                self._populate_default_model_combo()
            finally:
                self.default_model_combo.blockSignals(False)
            # Endpoints. Providers are global now so seeding from a
            # per-profile catalog no longer applies — the user adds
            # endpoints explicitly. We still materialise whatever's
            # already on the profile.
            self.endpoints_table.setRowCount(0)
            for model_name, endpoint in self._profile.model_endpoints.items():
                self._append_endpoint_row(model_name, endpoint)
            if self.endpoints_table.rowCount() == 0:
                self._append_endpoint_row("", None)
            _populate_pairs(self.env_table, list(self._profile.env_vars.items()))
            self._populate_plugins_tab()
            self._populate_mcp_servers_table()
            try:
                settings_text = json.dumps(
                    self._profile.claude_settings, indent=2, sort_keys=True
                )
            except (TypeError, ValueError):
                settings_text = ""
            self.settings_edit.setPlainText(settings_text)
            self._refresh_subagent_list()
            self._populate_container_tab()
        finally:
            self._populating = False
        # Auto-select first row in each table
        if self.endpoints_table.rowCount() > 0:
            self.endpoints_table.selectRow(0)
        if self.env_table.rowCount() > 0:
            self.env_table.selectRow(0)

    # ---- accept (validation + save) ----

    def _on_accept(self) -> None:
        # Snap-apply pending form edits. The endpoints + env-vars
        # tabs own their own row state (see ``_endpoints_tab.py``
        # and ``_env_vars_tab.py``); provider + key rows used to
        # live here too, but those tabs moved to
        # ``Claude > Settings`` (see
        # ``src/ui/claude/global_settings/``) and their snap-apply
        # blocks ran there.
        if (
            getattr(self, "_selected_endpoint_row", -1) >= 0
            and self._endpoint_form_values_diverge_from_row()
        ):
            self._on_endpoint_apply()
        if (
            getattr(self, "_selected_env_row", -1) >= 0
            and self._env_form_values_diverge_from_row()
        ):
            self._on_env_apply()

        # Validate name
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Profile name cannot be empty.")
            return
        self._profile.name = name
        self._profile.slug = slugify(name)

        # API Keys live in the global settings store now; the
        # profile editor only references them by name via the
        # provider's ``key_ref``. The default-key + uniqueness
        # invariants are enforced by GlobalSettingsDialog's own
        # accept handler — duplicating the check here would
        # briefly accept a malformed global state during the
        # window between Claude > Settings save and a profile
        # save, and we'd need a stale-key error path that the
        # user has no UI to fix from. Trust the global store.
        #
        # The ``_cred_edited_by_key`` / ``_key_renames`` dicts
        # used to be mutated here too; they're now owned by the
        # global settings dialog and never touched from this
        # class. ``_initial_key_names`` is kept on the instance
        # for symmetry with the global dialog (and so future
        # edits to the sensitive-header sanitizer can still find
        # the baseline) but is unused here.

        # Providers live in the global settings store now; the
        # profile editor only references them by name. Read the
        # registry from the global store for the validation checks
        # below — adding new providers is done in Claude > Settings.
        #
        # Resolution order, mirroring the live loader the main
        # window installs:
        #   1. ``self._global_settings_loader`` (a callable that
        #      returns the live ``GlobalSettings`` — the production
        #      app installs this so the dialog sees edits the user
        #      made in Claude > Settings without an intermediate
        #      save).
        #   2. ``self._global_settings_store`` (a direct
        #      ``GlobalSettingsStore`` instance — the test path;
        #      keeps the read inside the test's tmp dir rather than
        #      touching the user's real ``data_dir()``).
        #   3. The production ``data_dir()`` path. The widget never
        #      has to explicitly construct this — the imported
        #      factory inside the ``else`` branch does.
        if self._global_settings_loader is not None:
            global_settings = self._global_settings_loader()
        elif self._global_settings_store is not None:
            global_settings = self._global_settings_store.load()
        else:
            from src.profiles.global_settings import GlobalSettingsStore
            from src.proxy.paths import data_dir

            global_settings = GlobalSettingsStore(
                data_dir() / "claude_global.json"
            ).load()
        providers = global_settings.providers or {}
        if "default" not in providers:
            QMessageBox.warning(
                self,
                "Missing default provider",
                "A 'default' provider is required. Add one on the "
                "Claude > Settings > Providers tab before saving.",
            )
            return
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

        # Endpoints
        endpoints = self._collect_endpoints()
        if not endpoints:
            QMessageBox.warning(
                self,
                "No model endpoints",
                "Add at least one model endpoint so the proxy knows where to route.",
            )
            return
        stale_providers: list[str] = []
        for model, ep in endpoints.items():
            if ep.provider_name not in providers:
                stale_providers.append(f"{model} -> {ep.provider_name}")
        if stale_providers:
            QMessageBox.warning(
                self,
                "Missing provider references",
                "These model/endpoint rows reference a provider that "
                "no longer exists in the global registry. Add the "
                "provider on Claude > Settings > Providers or pick "
                "another provider, then save again.\n\n"
                f"Rows: {stale_providers!r}",
            )
            return
        # Validate model in provider catalog
        stale_models: list[str] = []
        for model, ep in endpoints.items():
            provider = providers.get(ep.provider_name)
            if provider is None:
                continue
            catalog = tuple(
                provider.get("available_models") or []
                if isinstance(provider, dict)
                else (getattr(provider, "available_models", ()) or ())
            )
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
                "Claude > Settings > Providers tab, or pick a "
                "different provider.\n\n"
                f"Models: {stale_models!r}",
            )
            return
        # Alias uniqueness
        alias_owner: dict[str, str] = {}
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
        # Soft warning: alias shadows real model
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

        # Bind / port / config_dir / session CWD are global now —
        # no per-profile copies. The General tab moved to
        # ``Claude > Settings``.

        # Env vars
        self._profile.env_vars = dict(_table_pairs(self.env_table))

        # MCP servers — the table is rebuilt into ``mcp_servers`` so
        # the on-disk ``.mcp.json`` mirrors exactly what the user sees.
        # ``enabled_plugins`` / ``marketplaces`` / ``blocked_plugins``
        # are mutated in-place by their tab handlers and need no
        # re-gather here.
        self._profile.mcp_servers = self._collect_mcp_servers()

        # Container — ``run_in_container`` + image + name. The
        # ``Container`` tab mutates ``self._profile`` in place via
        # ``_collect_container_tab``; we just call it here so the
        # accept handler commits the user's changes.
        self._collect_container_tab()

        # Claude settings (raw JSON) — merge the parsed JSON on top
        # of the dict we've accumulated so far. Replacing the dict
        # wholesale would silently drop writes from the Plugins /
        # MCP / Subagents tab handlers, so the JSON editor's
        # contents is treated as *additive*.
        #
        # **Order matters**: the Default-model combo (next block)
        # runs *after* this merge so it can override a stale
        # ``"model"`` entry the JSON text picked up at populate
        # time. Without that, picking ``(no default)`` would still
        # leave ``claude_settings["model"] = "<previous model>"``
        # because the JSON editor's text was snapshotted from
        # ``self._profile.claude_settings`` *before* the user
        # touched the combo. Form is the source of truth for
        # structured keys, JSON is the escape hatch for everything
        # else.
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
            merged = dict(self._profile.claude_settings)
            merged.update(parsed)
            self._profile.claude_settings = merged
        elif not self._profile.claude_settings:
            # Both the JSON editor and the form says nothing — keep
            # an empty dict rather than wiping whatever the form
            # handlers may have written.
            self._profile.claude_settings = {}

        # Default model — stored under ``claude_settings["model"]`` so
        # the per-profile ``settings.json`` writes the same key
        # Claude Code reads. Runs *after* the JSON-merge block above
        # so the combo's choice overrides any stale ``"model"`` key
        # the JSON text carried in from populate time. Picking
        # ``(no default)`` (userData ``None``) therefore reliably
        # clears the key and keeps ``ANTHROPIC_MODEL`` unset on
        # session launch.
        default_model: str | None = self.default_model_combo.currentData()
        if default_model is not None:
            default_model = str(default_model).strip() or None
        if not isinstance(self._profile.claude_settings, dict):
            self._profile.claude_settings = {}
        if default_model:
            self._profile.claude_settings["model"] = default_model
        else:
            self._profile.claude_settings.pop("model", None)

        self.accept()

    # ---- public API --------------------------------------------------------

    def to_profile(self) -> ClaudeProfile:
        """Return the edited profile (caller's responsibility to store it)."""
        return self._profile
