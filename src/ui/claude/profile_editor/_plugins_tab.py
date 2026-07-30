"""Plugins tab mixin for :class:`ProfileEditorDialog`.

Four sections inside one ``QScrollArea``:

1. **Enabled plugins** — toggle which ``plugin@marketplace`` refs
   Claude Code will load at session start. The on-disk
   ``settings.json:enabledPlugins`` dict drives Claude Code; this
   table is just a friendly editor for that dict.
2. **Marketplaces** — known marketplace sources (the content of
   ``plugins/known_marketplaces.json``). Add/Remove via a small
   modal that captures the source kind + URL/repo/path fields.
3. **Available plugins (discovery)** — ``QTreeWidget`` with one
   top-level node per registered marketplace. Children are the
   plugins in that marketplace's manifest, fetched over HTTP
   (``urllib.request`` — no new dependency). Each child row has
   Install / Enable / Disable buttons that shell out to the
   ``claude`` CLI; output is surfaced in a ``QMessageBox``.
4. **Blocked plugins** — list of ``plugin@marketplace`` refs that
   the user has explicitly blacklisted (matches Claude Code's
   ``plugins/blocklist.json`` schema).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.models import MarketplaceSource
from src.ui.claude.profile_editor._dialogs import _MarketplaceEditorDialog


# Network timeout (seconds) for fetching a marketplace manifest.
# Short on purpose — the user is waiting on this in the GUI, and a
# hung request blocks the discovery tree. 8s is enough for normal
# GitHub raw fetches; misbehaving hosts will surface a timeout
# error rather than lock the dialog.
_FETCH_TIMEOUT = 8.0


@dataclass
class _MarketplaceListing:
    """One fetched marketplace + its plugin list (transient, not persisted)."""

    marketplace_name: str
    source: MarketplaceSource
    plugins: list[dict]   # raw plugin entries from the manifest
    error: str | None = None


class PluginsTabMixin:
    def _build_plugins_tab(self) -> None:
        """Tab 7: enabled plugins, marketplaces, discovery, blocklist.

        Wrapped in a ``QScrollArea`` because the four sections stack
        vertically and the discovery tree can grow tall when a
        marketplace has dozens of plugins. The dialog itself stays
        compact; this tab's content scrolls.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        help_label = QLabel(
            "Manage Claude Code plugins for this profile. <b>Enabled</b> "
            "controls which plugins load at session start. "
            "<b>Marketplaces</b> lists registered plugin sources. "
            "<b>Available plugins</b> fetches each marketplace's manifest "
            "and lets you install plugins via the <code>claude</code> "
            "CLI. <b>Blocked</b> lists plugins the user has blacklisted."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(help_label)

        # ---- 1. Enabled plugins --------------------------------------
        enabled_box = QGroupBox("Enabled plugins")
        enabled_layout = QVBoxLayout(enabled_box)
        enabled_layout.setContentsMargins(8, 8, 8, 8)
        self.plugins_enabled_table = QTableWidget(0, 2)
        self.plugins_enabled_table.setHorizontalHeaderLabels(
            ["Plugin reference", "Enabled"]
        )
        self.plugins_enabled_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.plugins_enabled_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.plugins_enabled_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.plugins_enabled_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.plugins_enabled_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.plugins_enabled_table.verticalHeader().setVisible(False)
        self.plugins_enabled_table.setMinimumHeight(100)
        enabled_layout.addWidget(self.plugins_enabled_table)

        enabled_btn_row = QHBoxLayout()
        enabled_add_btn = QPushButton("Add…")
        enabled_add_btn.clicked.connect(self._on_plugin_enabled_add)
        enabled_remove_btn = QPushButton("Remove Selected")
        enabled_remove_btn.clicked.connect(self._on_plugin_enabled_remove)
        self.plugins_enabled_remove_btn = enabled_remove_btn
        enabled_btn_row.addWidget(enabled_add_btn)
        enabled_btn_row.addWidget(enabled_remove_btn)
        enabled_btn_row.addStretch(1)
        enabled_btn_container = QWidget()
        enabled_btn_container.setLayout(enabled_btn_row)
        enabled_layout.addWidget(enabled_btn_container)
        tab_layout.addWidget(enabled_box)

        # ---- 2. Marketplaces ----------------------------------------
        mk_box = QGroupBox("Marketplaces")
        mk_layout = QVBoxLayout(mk_box)
        mk_layout.setContentsMargins(8, 8, 8, 8)
        self.marketplaces_table = QTableWidget(0, 2)
        self.marketplaces_table.setHorizontalHeaderLabels(["Name", "Source"])
        self.marketplaces_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.marketplaces_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.marketplaces_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.marketplaces_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.marketplaces_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.marketplaces_table.verticalHeader().setVisible(False)
        self.marketplaces_table.setMinimumHeight(80)
        mk_layout.addWidget(self.marketplaces_table)
        mk_btn_row = QHBoxLayout()
        mk_add_btn = QPushButton("Add…")
        mk_add_btn.clicked.connect(self._on_marketplace_add)
        mk_edit_btn = QPushButton("Edit…")
        mk_edit_btn.clicked.connect(self._on_marketplace_edit)
        mk_remove_btn = QPushButton("Remove")
        mk_remove_btn.clicked.connect(self._on_marketplace_remove)
        self.marketplaces_remove_btn = mk_remove_btn
        self.marketplaces_edit_btn = mk_edit_btn
        mk_btn_row.addWidget(mk_add_btn)
        mk_btn_row.addWidget(mk_edit_btn)
        mk_btn_row.addWidget(mk_remove_btn)
        mk_btn_row.addStretch(1)
        mk_btn_container = QWidget()
        mk_btn_container.setLayout(mk_btn_row)
        mk_layout.addWidget(mk_btn_container)
        tab_layout.addWidget(mk_box)

        # ---- 3. Discovery -------------------------------------------
        disc_box = QGroupBox("Available plugins (discovery)")
        disc_layout = QVBoxLayout(disc_box)
        disc_layout.setContentsMargins(8, 8, 8, 8)
        self.plugins_tree = QTreeWidget()
        self.plugins_tree.setHeaderLabels(
            ["Plugin / Marketplace", "Description", "Version"]
        )
        self.plugins_tree.setRootIsDecorated(True)
        self.plugins_tree.setAlternatingRowColors(True)
        self.plugins_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.plugins_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.plugins_tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.plugins_tree.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.plugins_tree.setMinimumHeight(180)
        disc_layout.addWidget(self.plugins_tree)

        disc_btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh marketplaces")
        refresh_btn.clicked.connect(self._refresh_discovery_tree)
        install_btn = QPushButton("Install…")
        install_btn.clicked.connect(self._on_plugin_install)
        enable_btn = QPushButton("Enable")
        enable_btn.clicked.connect(self._on_plugin_toggle_enable)
        disable_btn = QPushButton("Disable")
        disable_btn.clicked.connect(self._on_plugin_toggle_disable)
        uninstall_btn = QPushButton("Uninstall")
        uninstall_btn.clicked.connect(self._on_plugin_uninstall)
        for btn in (refresh_btn, install_btn, enable_btn, disable_btn, uninstall_btn):
            disc_btn_row.addWidget(btn)
        disc_btn_row.addStretch(1)
        disc_btn_container = QWidget()
        disc_btn_container.setLayout(disc_btn_row)
        disc_layout.addWidget(disc_btn_container)
        tab_layout.addWidget(disc_box)

        # ---- 4. Blocklist -------------------------------------------
        block_box = QGroupBox("Blocked plugins")
        block_layout = QVBoxLayout(block_box)
        block_layout.setContentsMargins(8, 8, 8, 8)
        self.plugins_blocked_list = QListWidget()
        self.plugins_blocked_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.plugins_blocked_list.setMinimumHeight(80)
        block_layout.addWidget(self.plugins_blocked_list)
        block_btn_row = QHBoxLayout()
        block_add_btn = QPushButton("Add…")
        block_add_btn.clicked.connect(self._on_blocked_add)
        block_remove_btn = QPushButton("Remove Selected")
        block_remove_btn.clicked.connect(self._on_blocked_remove)
        block_btn_row.addWidget(block_add_btn)
        block_btn_row.addWidget(block_remove_btn)
        block_btn_row.addStretch(1)
        block_btn_container = QWidget()
        block_btn_container.setLayout(block_btn_row)
        block_layout.addWidget(block_btn_container)
        tab_layout.addWidget(block_box)

        # Wrap in a scroll area so the four sections don't push the
        # rest of the dialog off-screen.
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setWidget(tab)
        outer.setFrameShape(QScrollArea.Shape.NoFrame)
        self.tabs.addTab(outer, "Plugins")

        # In-memory cache of fetched marketplace listings. Populated
        # by ``_refresh_discovery_tree``; consumed by the per-row
        # action handlers (so we don't refetch on every click).
        self._marketplace_listings: dict[str, _MarketplaceListing] = {}

    # ---- population ----------------------------------------------------

    def _populate_plugins_tab(self) -> None:
        """Fill the four tables from ``self._profile``.

        The discovery tree starts empty — the user clicks
        "Refresh marketplaces" to populate it (network fetch +
        optional subprocess). We never auto-fetch on dialog open
        because that would surprise the user with a long delay.
        """
        self._populate_plugins_enabled_table()
        self._populate_marketplaces_table()
        self._populate_blocked_list()
        self.plugins_tree.clear()
        self._marketplace_listings.clear()

    def _populate_plugins_enabled_table(self) -> None:
        self.plugins_enabled_table.setRowCount(0)
        # Sort by reference so the row order is stable across opens.
        for ref in sorted(self._profile.enabled_plugins):
            self._append_plugins_enabled_row(ref, self._profile.enabled_plugins[ref])

    def _append_plugins_enabled_row(self, ref: str, enabled: bool) -> None:
        row = self.plugins_enabled_table.rowCount()
        self.plugins_enabled_table.insertRow(row)
        ref_item = QTableWidgetItem(ref)
        ref_item.setFlags(ref_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        ref_item.setData(Qt.ItemDataRole.UserRole, enabled)
        self.plugins_enabled_table.setItem(row, 0, ref_item)
        checkbox = QCheckBox()
        checkbox.setChecked(enabled)
        checkbox.setToolTip("Toggle whether Claude Code loads this plugin at session start")
        checkbox.toggled.connect(
            lambda checked, r=row: self._on_plugin_enabled_toggled(r, checked)
        )
        # Center the checkbox in the cell.
        cell_widget = QWidget()
        cell_layout = QHBoxLayout(cell_widget)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.addStretch(1)
        cell_layout.addWidget(checkbox)
        cell_layout.addStretch(1)
        self.plugins_enabled_table.setCellWidget(row, 1, cell_widget)

    def _populate_marketplaces_table(self) -> None:
        self.marketplaces_table.setRowCount(0)
        for name in sorted(self._profile.marketplaces):
            src = self._profile.marketplaces[name]
            self._append_marketplace_row(name, src)

    def _append_marketplace_row(self, name: str, src: MarketplaceSource) -> None:
        row = self.marketplaces_table.rowCount()
        self.marketplaces_table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, src)
        self.marketplaces_table.setItem(row, 0, name_item)
        # Render the source as a human-friendly string.
        if src.source == "github" and src.repo:
            desc = f"github: {src.repo}"
        elif src.source in ("git", "url") and src.url:
            desc = f"{src.source}: {src.url}"
        elif src.source == "git-subdir" and src.url:
            desc = f"git-subdir: {src.url} ({src.path or '?'})"
        else:
            desc = src.source
        if src.ref:
            desc += f"  ref={src.ref}"
        src_item = QTableWidgetItem(desc)
        src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.marketplaces_table.setItem(row, 1, src_item)

    def _populate_blocked_list(self) -> None:
        self.plugins_blocked_list.clear()
        for ref in self._profile.blocked_plugins:
            self.plugins_blocked_list.addItem(QListWidgetItem(ref))

    # ---- enabled plugins table slots ---------------------------------

    def _on_plugin_enabled_toggled(self, row: int, checked: bool) -> None:
        ref_item = self.plugins_enabled_table.item(row, 0)
        if ref_item is None:
            return
        ref_item.setData(Qt.ItemDataRole.UserRole, checked)

    def _on_plugin_enabled_add(self) -> None:
        ref, ok = QInputDialog.getText(
            self,
            "Enable plugin",
            "Plugin reference (plugin@marketplace):",
        )
        if not ok:
            return
        ref = ref.strip()
        if not ref or "@" not in ref:
            QMessageBox.warning(
                self,
                "Invalid reference",
                "Plugin references must be of the form 'plugin@marketplace'.",
            )
            return
        if ref in self._profile.enabled_plugins:
            QMessageBox.information(
                self, "Already enabled", f"{ref} is already in the enabled list."
            )
            return
        self._profile.enabled_plugins[ref] = True
        self._append_plugins_enabled_row(ref, True)

    def _on_plugin_enabled_remove(self) -> None:
        row = self.plugins_enabled_table.currentRow()
        if row < 0:
            return
        ref_item = self.plugins_enabled_table.item(row, 0)
        if ref_item is None:
            return
        ref = ref_item.text()
        self.plugins_enabled_table.removeRow(row)
        self._profile.enabled_plugins.pop(ref, None)

    # ---- marketplaces table slots -------------------------------------

    def _on_marketplace_add(self) -> None:
        dlg = _MarketplaceEditorDialog("", source=None, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, src = dlg.to_source()
        if name in self._profile.marketplaces:
            QMessageBox.warning(
                self,
                "Duplicate name",
                f"A marketplace named {name!r} already exists.",
            )
            return
        self._profile.marketplaces[name] = src
        self._append_marketplace_row(name, src)

    def _on_marketplace_edit(self) -> None:
        row = self.marketplaces_table.currentRow()
        if row < 0:
            return
        name_item = self.marketplaces_table.item(row, 0)
        if name_item is None:
            return
        old_name = name_item.text()
        src = name_item.data(Qt.ItemDataRole.UserRole)
        dlg = _MarketplaceEditorDialog(old_name, source=src, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name, new_src = dlg.to_source()
        if new_name != old_name:
            # Removing then re-adding reorders the table — but since
            # the table is sorted on repopulate, we just rewrite the
            # in-memory dict and let the next repopulate handle the
            # ordering. ``_on_accept`` will write the profile from
            # ``_profile.marketplaces`` directly anyway.
            self._profile.marketplaces.pop(old_name, None)
        if new_name != old_name and new_name in self._profile.marketplaces:
            QMessageBox.warning(
                self,
                "Duplicate name",
                f"A marketplace named {new_name!r} already exists.",
            )
            return
        self._profile.marketplaces[new_name] = new_src
        # Refresh the table — easiest path is to repopulate fully.
        self._populate_marketplaces_table()

    def _on_marketplace_remove(self) -> None:
        row = self.marketplaces_table.currentRow()
        if row < 0:
            return
        name_item = self.marketplaces_table.item(row, 0)
        if name_item is None:
            return
        name = name_item.text()
        self.marketplaces_table.removeRow(row)
        self._profile.marketplaces.pop(name, None)

    # ---- blocklist slots ---------------------------------------------

    def _on_blocked_add(self) -> None:
        ref, ok = QInputDialog.getText(
            self,
            "Block plugin",
            "Plugin reference to block (plugin@marketplace):",
        )
        if not ok:
            return
        ref = ref.strip()
        if not ref or "@" not in ref:
            QMessageBox.warning(
                self,
                "Invalid reference",
                "Plugin references must be of the form 'plugin@marketplace'.",
            )
            return
        if ref in self._profile.blocked_plugins:
            QMessageBox.information(
                self, "Already blocked", f"{ref} is already in the blocklist."
            )
            return
        self._profile.blocked_plugins.append(ref)
        self.plugins_blocked_list.addItem(ref)

    def _on_blocked_remove(self) -> None:
        row = self.plugins_blocked_list.currentRow()
        if row < 0:
            return
        item = self.plugins_blocked_list.takeItem(row)
        ref = item.text() if item else ""
        if ref and ref in self._profile.blocked_plugins:
            self._profile.blocked_plugins.remove(ref)

    # ---- discovery tree ------------------------------------------------

    def _refresh_discovery_tree(self) -> None:
        """Fetch every registered marketplace manifest and populate the tree.

        Each marketplace becomes a top-level node; its plugins become
        children. Failures (timeout, 404, parse error) are captured
        per-marketplace and shown as the node's tooltip rather than
        blocking the rest of the refresh.
        """
        self.plugins_tree.clear()
        self._marketplace_listings.clear()
        if not self._profile.marketplaces:
            empty = QTreeWidgetItem(["(no marketplaces configured — add one above)"])
            self.plugins_tree.addTopLevelItem(empty)
            return
        for name, src in self._profile.marketplaces.items():
            listing = self._fetch_marketplace(name, src)
            self._marketplace_listings[name] = listing
            top = QTreeWidgetItem([name, listing.error or "", ""])
            top.setData(0, Qt.ItemDataRole.UserRole, ("marketplace", name))
            if listing.error:
                top.setToolTip(1, listing.error)
            else:
                for entry in listing.plugins:
                    pname = str(entry.get("name") or "?")
                    desc = str(entry.get("description") or "")
                    version = str(entry.get("version") or "")
                    child = QTreeWidgetItem([pname, desc, version])
                    # Stable key the install handlers read back.
                    ref = f"{pname}@{name}"
                    child.setData(0, Qt.ItemDataRole.UserRole, ("plugin", ref))
                    top.addChild(child)
            self.plugins_tree.addTopLevelItem(top)
            top.setExpanded(True)

    def _fetch_marketplace(self, name: str, src: MarketplaceSource) -> _MarketplaceListing:
        """Fetch one marketplace's manifest, returning a ``_MarketplaceListing``.

        Source URLs depend on ``source.kind``:

        - ``github``: ``https://raw.githubusercontent.com/<repo>/<ref-or-main>/.claude-plugin/marketplace.json``
        - ``git`` / ``url``: ``<url>`` (assumed to point at the manifest directly; this isn't well-defined for arbitrary git URLs so the caller may need to configure the path).
        - ``git-subdir``: ``https://raw.githubusercontent.com/<owner>/<repo>/<ref-or-main>/<path>/.claude-plugin/marketplace.json``

        Returns the listing with ``error`` set on any failure so
        the caller can render it in the tree.
        """
        url = self._resolve_manifest_url(src)
        if not url:
            return _MarketplaceListing(
                marketplace_name=name,
                source=src,
                plugins=[],
                error="Could not resolve a manifest URL for this source.",
            )
        try:
            req = Request(url, headers={"User-Agent": "SharkDrown-ProfileEditor/1.0"})
            with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError) as e:
            return _MarketplaceListing(
                marketplace_name=name,
                source=src,
                plugins=[],
                error=f"Fetch failed: {e}",
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return _MarketplaceListing(
                marketplace_name=name,
                source=src,
                plugins=[],
                error=f"Invalid JSON: {e}",
            )
        plugins_raw = data.get("plugins") if isinstance(data, dict) else None
        if not isinstance(plugins_raw, list):
            return _MarketplaceListing(
                marketplace_name=name,
                source=src,
                plugins=[],
                error="Manifest has no 'plugins' array.",
            )
        return _MarketplaceListing(
            marketplace_name=name,
            source=src,
            plugins=[p for p in plugins_raw if isinstance(p, dict)],
        )

    def _resolve_manifest_url(self, src: MarketplaceSource) -> str | None:
        """Translate a marketplace source into a fetchable manifest URL."""
        ref = src.ref or "main"
        if src.source == "github" and src.repo:
            return (
                f"https://raw.githubusercontent.com/{src.repo}/"
                f"{ref}/.claude-plugin/marketplace.json"
            )
        if src.source == "git-subdir" and src.repo and src.path:
            # ``git-subdir`` markets split into ``owner/repo`` + ``path``
            # when fetched via GitHub's raw CDN. ``url`` may carry the
            # full git URL but the manifest still lives on the raw
            # CDN at that path.
            return (
                f"https://raw.githubusercontent.com/{src.repo}/"
                f"{ref}/{src.path}/.claude-plugin/marketplace.json"
            )
        if src.source in ("git", "url") and src.url:
            # Caller has supplied a direct manifest URL.
            return src.url
        return None

    # ---- plugin action handlers ---------------------------------------

    def _selected_plugin_ref(self) -> tuple[str, str] | None:
        """Return ``(action, ref)`` for the current tree selection.

        ``action`` is ``"plugin"`` (a child node) or
        ``"marketplace"`` (a top-level node). Returns ``None`` if
        nothing relevant is selected.
        """
        items = self.plugins_tree.selectedItems()
        if not items:
            return None
        item = items[0]
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return None
        return (str(payload[0]), str(payload[1]))

    def _on_plugin_install(self) -> None:
        sel = self._selected_plugin_ref()
        if not sel or sel[0] != "plugin":
            QMessageBox.information(
                self,
                "Select a plugin",
                "Pick a plugin in the tree before clicking Install.",
            )
            return
        ref = sel[1]
        ok = self._run_claude_subprocess(
            ["plugin", "install", ref],
            success_msg=f"Installed {ref}.",
            failure_msg=f"Failed to install {ref}.",
        )
        if ok:
            # Auto-enable on successful install — that's the UX most
            # users want. ``enabled_plugins`` is the in-memory dict
            # the OK handler will write out.
            self._profile.enabled_plugins[ref] = True
            self._populate_plugins_enabled_table()

    def _on_plugin_toggle_enable(self) -> None:
        self._toggle_plugin_enabled(True)

    def _on_plugin_toggle_disable(self) -> None:
        self._toggle_plugin_enabled(False)

    def _toggle_plugin_enabled(self, enable: bool) -> None:
        sel = self._selected_plugin_ref()
        if not sel or sel[0] != "plugin":
            QMessageBox.information(
                self,
                "Select a plugin",
                "Pick a plugin in the tree before toggling enable state.",
            )
            return
        ref = sel[1]
        # Toggling on the registry is a Claude Code action; we mirror
        # the result in ``enabled_plugins`` regardless of CLI exit so
        # the user sees their intent reflected immediately.
        ok = self._run_claude_subprocess(
            ["plugin", "enable" if enable else "disable", ref],
            success_msg=f"{'Enabled' if enable else 'Disabled'} {ref}.",
            failure_msg=f"Failed to {'enable' if enable else 'disable'} {ref}.",
        )
        self._profile.enabled_plugins[ref] = enable
        self._populate_plugins_enabled_table()

    def _on_plugin_uninstall(self) -> None:
        sel = self._selected_plugin_ref()
        if not sel or sel[0] != "plugin":
            QMessageBox.information(
                self,
                "Select a plugin",
                "Pick a plugin in the tree before clicking Uninstall.",
            )
            return
        ref = sel[1]
        confirm = QMessageBox.question(
            self,
            "Uninstall plugin",
            f"Uninstall {ref}? This removes it from this profile's installed "
            "plugins registry; Claude Code will stop loading it.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        ok = self._run_claude_subprocess(
            ["plugin", "uninstall", ref],
            success_msg=f"Uninstalled {ref}.",
            failure_msg=f"Failed to uninstall {ref}.",
        )
        if ok:
            self._profile.enabled_plugins.pop(ref, None)
            self._populate_plugins_enabled_table()

    def _run_claude_subprocess(
        self,
        args: list[str],
        *,
        success_msg: str,
        failure_msg: str,
    ) -> bool:
        """Shell out to ``claude <args>``, surface output in a dialog.

        Returns ``True`` on exit-code 0. ``subprocess.run`` blocks
        the UI thread — for short ``claude plugin install`` calls
        this is fine; a future iteration could move this onto a
        ``QThread`` if latency becomes a complaint.
        """
        try:
            result = subprocess.run(
                ["claude", *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            QMessageBox.critical(
                self,
                "claude CLI not found",
                "The 'claude' executable was not found on PATH. "
                "Install Claude Code or adjust PATH so SharkDrown can "
                "shell out to it.",
            )
            return False
        except subprocess.TimeoutExpired:
            QMessageBox.critical(
                self,
                "Timeout",
                f"claude {' '.join(args)} didn't complete within 60s.",
            )
            return False
        if result.returncode == 0:
            QMessageBox.information(self, "OK", success_msg)
            return True
        detail = (result.stderr or result.stdout or "").strip() or "(no output)"
        QMessageBox.critical(self, "Failed", f"{failure_msg}\n\n{detail}")
        return False