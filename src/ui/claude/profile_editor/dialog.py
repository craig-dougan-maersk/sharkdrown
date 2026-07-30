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
    QLabel,
    QMessageBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.credential_store import CredentialStore
from src.profiles.models import ClaudeProfile, ModelEndpoint, Provider, slugify

from src.ui.claude.profile_editor._general_tab import GeneralTabMixin
from src.ui.claude.profile_editor._endpoints_tab import EndpointsTabMixin
from src.ui.claude.profile_editor._providers_tab import ProvidersTabMixin
from src.ui.claude.profile_editor._api_keys_tab import ApiKeysTabMixin
from src.ui.claude.profile_editor._env_vars_tab import EnvVarsTabMixin
from src.ui.claude.profile_editor._settings_subagents_tab import SettingsSubagentsTabMixin
from src.ui.claude.profile_editor._plugins_tab import PluginsTabMixin
from src.ui.claude.profile_editor._mcp_servers_tab import McpServersTabMixin


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
    GeneralTabMixin,
    EndpointsTabMixin,
    ProvidersTabMixin,
    ApiKeysTabMixin,
    EnvVarsTabMixin,
    SettingsSubagentsTabMixin,
    PluginsTabMixin,
    McpServersTabMixin,
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Claude Profile — {profile.name}")
        self.resize(720, 720)
        self._profile = copy.deepcopy(profile)
        self._creds = credential_store
        self._cred_edited_by_key: dict[str, str | bool] = {}
        self._key_renames: dict[str, str] = {}
        self._initial_key_names: set[str] = set(self._creds.all_refs().keys())
        self._populating = False
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Configure a Claude Code session profile. The proxy will route "
            "requests for each model to its configured endpoint. Sessions "
            "always go through the embedded proxy at localhost."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_endpoints_tab()
        self._build_providers_tab()
        self._build_api_key_tab()
        self._build_env_vars_tab()
        self._build_plugins_tab()
        self._build_mcp_servers_tab()
        self._build_settings_tab()
        self._build_subagents_tab()

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
            self.cwd_edit.setText(self._profile.session_cwd or "")
            self.bind_edit.setText(self._profile.bind_address or "127.0.0.1")
            self.port_edit.setValue(int(self._profile.port or 0))
            self.config_dir_edit.setText(self._profile.config_dir or "")
            # API Keys first (endpoint combos need key names)
            self._populate_keys_table()
            # Providers next (endpoint combos need provider names)
            self._populate_providers_table()
            # Endpoints (depends on both keys and providers)
            self._maybe_seed_endpoints_from_provider_catalog()
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
        finally:
            self._populating = False
        # Auto-select first row in each table
        if self.keys_table.rowCount() > 0:
            self.keys_table.selectRow(0)
        if self.providers_table.rowCount() > 0:
            self.providers_table.selectRow(0)
        if self.endpoints_table.rowCount() > 0:
            self.endpoints_table.selectRow(0)
        if self.env_table.rowCount() > 0:
            self.env_table.selectRow(0)

    # ---- accept (validation + save) ----

    def _on_accept(self) -> None:
        # Snap-apply pending form edits
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

        # Validate name
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Profile name cannot be empty.")
            return
        self._profile.name = name
        self._profile.slug = slugify(name)

        # API Keys: validate uniqueness + persist
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
        # Process deletions
        for ref_name in list(self._cred_edited_by_key.keys()):
            if ref_name.startswith("__deleted__"):
                deleted_name = ref_name[len("__deleted__"):]
                try:
                    self._creds.delete(deleted_name)
                except (OSError, json.JSONDecodeError):
                    pass
        # Apply renames
        for new_name, old_name in self._key_renames.items():
            try:
                existing = self._creds.get(old_name)
                if existing is not None:
                    self._creds.set(new_name, existing)
                self._creds.delete(old_name)
            except (OSError, json.JSONDecodeError):
                pass
        # Apply value edits
        for ref_name, value in list(self._cred_edited_by_key.items()):
            if ref_name.startswith("__deleted__"):
                continue
            if not isinstance(value, str):
                continue
            if value == "":
                continue
            try:
                self._creds.set(ref_name, value)
            except (OSError, json.JSONDecodeError):
                pass

        # Providers
        providers = self._collect_providers()
        if "default" not in providers:
            QMessageBox.warning(
                self,
                "Missing default provider",
                "A 'default' provider is required. Re-add it on the "
                "Providers tab before saving.",
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
        self._profile.providers = providers

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
                "no longer exists. The row's Provider dropdown has "
                "been reset to '(no provider)' — re-add the provider "
                "on the Providers tab or pick another provider, then "
                "save again.\n\n"
                f"Rows: {stale_providers!r}",
            )
            return
        # Validate model in provider catalog
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

        # Default model
        default_model: str | None = self.default_model_combo.currentData()
        if default_model is not None:
            default_model = str(default_model).strip() or None
        self._profile.default_model = default_model

        # Session CWD
        cwd = self.cwd_edit.text().strip() or None
        self._profile.session_cwd = cwd

        # Proxy / network
        bind = self.bind_edit.text().strip() or "127.0.0.1"
        self._profile.bind_address = bind
        self._profile.port = int(self.port_edit.value())
        self._profile.config_dir = self.config_dir_edit.text().strip()

        # Env vars
        self._profile.env_vars = dict(_table_pairs(self.env_table))

        # MCP servers — the table is rebuilt into ``mcp_servers`` so
        # the on-disk ``.mcp.json`` mirrors exactly what the user sees.
        # ``enabled_plugins`` / ``marketplaces`` / ``blocked_plugins``
        # are mutated in-place by their tab handlers and need no
        # re-gather here.
        self._profile.mcp_servers = self._collect_mcp_servers()

        # Claude settings (raw JSON)
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

    # ---- public API --------------------------------------------------------

    def to_profile(self) -> ClaudeProfile:
        """Return the edited profile (caller's responsibility to store it)."""
        return self._profile
