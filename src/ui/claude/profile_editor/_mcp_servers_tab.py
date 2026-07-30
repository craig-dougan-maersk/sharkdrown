"""MCP Servers tab mixin for :class:`ProfileEditorDialog`.

Renders the per-profile ``.mcp.json`` config as a table + form, with
the table listing every server (name, transport, command-or-url) and
the form below capturing the typed fields plus a raw-JSON fallback
for any Claude-Code-side keys we don't model.

The data model is :class:`src.profiles.models.McpServer` — a frozen
dataclass — so every UI mutation creates a fresh instance, matching
the existing ``Provider`` / ``ModelEndpoint`` discipline.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
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
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.profiles.models import McpServer


# Allowed transport values; ordered to match the Claude Code docs
# (stdio is the default and overwhelmingly the most common).
_TRANSPORTS = ("stdio", "sse", "http")


class McpServersTabMixin:
    def _build_mcp_servers_tab(self) -> None:
        """Tab 8: per-profile ``.mcp.json`` editor.

        Table at top (read-only, row-select), form below. The form's
        Apply button is enabled only when its values diverge from the
        selected row — same pattern as the API Keys and Providers
        tabs.

        The raw-JSON textarea captures everything Claude Code may add
        to its server schema (``headers``, ``oauthClientId``, etc.)
        that we don't model directly. It's validated on Apply and on
        OK so a malformed bag fails the dialog rather than getting
        silently dropped.
        """
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        help_label = QLabel(
            "MCP (Model Context Protocol) servers launched by Claude Code "
            "for this profile. Saved to <code>.mcp.json</code> inside the "
            "profile's <code>CLAUDE_CONFIG_DIR</code>. <b>stdio</b> servers "
            "use <em>command</em> + <em>args</em> + <em>env</em>; "
            "<b>sse</b> and <b>http</b> servers use <em>url</em>. Use the "
            "raw-JSON block at the bottom for any field Claude Code adds "
            "that the form doesn't model yet (e.g. <code>headers</code>, "
            "<code>oauthClientId</code>)."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(help_label)

        # ---- Server table ----------------------------------------------
        self.mcp_servers_table = QTableWidget(0, 3)
        self.mcp_servers_table.setHorizontalHeaderLabels(
            ["Name", "Transport", "Command / URL"]
        )
        self.mcp_servers_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.mcp_servers_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.mcp_servers_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.mcp_servers_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.mcp_servers_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.mcp_servers_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.mcp_servers_table.verticalHeader().setVisible(False)
        self.mcp_servers_table.itemSelectionChanged.connect(
            self._on_mcp_server_selection_changed
        )
        self.mcp_servers_table.setMinimumHeight(140)
        self.mcp_servers_table_scroll = self._wrap_in_scroll(
            self.mcp_servers_table, max_height=180
        )
        tab_layout.addWidget(self.mcp_servers_table_scroll)

        # ---- Add / Remove buttons ---------------------------------------
        mcp_btns = QHBoxLayout()
        mcp_add = QPushButton("Add…")
        mcp_add.clicked.connect(self._on_mcp_server_add)
        mcp_remove = QPushButton("Remove Selected")
        mcp_remove.clicked.connect(self._on_mcp_server_remove)
        self.mcp_remove_btn = mcp_remove
        mcp_btns.addWidget(mcp_add)
        mcp_btns.addWidget(mcp_remove)
        mcp_btns.addStretch(1)
        mcp_btn_container = QWidget()
        mcp_btn_container.setLayout(mcp_btns)
        tab_layout.addWidget(mcp_btn_container)

        # ---- Row editor form -------------------------------------------
        # Tracks which table row the form mirrors. ``-1`` = no row.
        self._selected_mcp_server_row: int = -1
        # Snapshot of the row's values at form-load time so we can
        # tell when the form has diverged and enable Apply / Revert.
        self._mcp_server_row_snapshot: tuple | None = None

        editor_box = QGroupBox("Edit selected server")
        editor_layout = QFormLayout(editor_box)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        self.mcp_name_edit = QLineEdit()
        self.mcp_name_edit.setPlaceholderText("e.g. filesystem")
        editor_layout.addRow("Name:", self.mcp_name_edit)

        self.mcp_transport_combo = QComboBox()
        for t in _TRANSPORTS:
            self.mcp_transport_combo.addItem(t)
        self.mcp_transport_combo.currentTextChanged.connect(
            self._on_mcp_transport_changed
        )
        editor_layout.addRow("Transport:", self.mcp_transport_combo)

        self.mcp_command_edit = QLineEdit()
        self.mcp_command_edit.setPlaceholderText(
            "/usr/local/bin/mcp-server or PATH-resolvable name"
        )
        editor_layout.addRow("Command:", self.mcp_command_edit)

        self.mcp_args_edit = QLineEdit()
        self.mcp_args_edit.setPlaceholderText(
            "space-separated args, e.g. --stdio --port 8080"
        )
        editor_layout.addRow("Args:", self.mcp_args_edit)

        self.mcp_url_edit = QLineEdit()
        self.mcp_url_edit.setPlaceholderText("https://example.com/mcp")
        editor_layout.addRow("URL (sse/http):", self.mcp_url_edit)

        self.mcp_env_edit = QLineEdit()
        self.mcp_env_edit.setPlaceholderText(
            "comma-separated KEY=VALUE, e.g. API_KEY=abc,REGION=eu"
        )
        editor_layout.addRow("Env:", self.mcp_env_edit)

        self.mcp_extra_edit = QPlainTextEdit()
        mono = self.mcp_extra_edit.font()
        mono.setFamily(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        )
        self.mcp_extra_edit.setFont(mono)
        self.mcp_extra_edit.setPlaceholderText(
            '{\n  "headers": {"Authorization": "Bearer ..."}\n}'
        )
        self.mcp_extra_edit.setMaximumHeight(120)
        editor_layout.addRow("Extra (JSON):", self.mcp_extra_edit)

        for w in (
            self.mcp_name_edit,
            self.mcp_command_edit,
            self.mcp_args_edit,
            self.mcp_url_edit,
            self.mcp_env_edit,
        ):
            w.textChanged.connect(self._on_mcp_server_form_changed)
        self.mcp_extra_edit.textChanged.connect(self._on_mcp_server_form_changed)

        btn_row = QHBoxLayout()
        self.mcp_apply_btn = QPushButton("Apply")
        self.mcp_apply_btn.setEnabled(False)
        self.mcp_apply_btn.clicked.connect(self._on_mcp_server_apply)
        self.mcp_revert_btn = QPushButton("Revert")
        self.mcp_revert_btn.setEnabled(False)
        self.mcp_revert_btn.clicked.connect(self._on_mcp_server_revert)
        btn_row.addStretch(1)
        btn_row.addWidget(self.mcp_apply_btn)
        btn_row.addWidget(self.mcp_revert_btn)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        editor_layout.addRow("", btn_container)

        self.mcp_form_status = QLabel("Select a row to edit")
        self.mcp_form_status.setStyleSheet("color: gray; font-size: 11px;")
        editor_layout.addRow("", self.mcp_form_status)

        self._set_mcp_server_form_enabled(False)

        tab_layout.addWidget(editor_box)
        tab_layout.addStretch(1)

        self.tabs.addTab(tab, "MCP Servers")

    # ---- population -----------------------------------------------------

    def _populate_mcp_servers_table(self) -> None:
        """Fill the servers table from ``self._profile.mcp_servers``.

        Sorted by name so the table order is stable across opens.
        UserRole on the name column carries the :class:`McpServer`
        instance so ``_on_mcp_server_selection_changed`` can pull it
        back without re-parsing the row.
        """
        self.mcp_servers_table.setRowCount(0)
        for name in sorted(self._profile.mcp_servers):
            srv = self._profile.mcp_servers[name]
            self._append_mcp_server_row(srv)

    def _append_mcp_server_row(self, srv: McpServer) -> None:
        """Insert one row at the bottom of the servers table."""
        row = self.mcp_servers_table.rowCount()
        self.mcp_servers_table.insertRow(row)
        name_item = QTableWidgetItem(srv.name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        # UserRole carries the full McpServer instance — it's the
        # source of truth when populating the form on selection.
        name_item.setData(Qt.ItemDataRole.UserRole, srv)
        self.mcp_servers_table.setItem(row, 0, name_item)

        transport_item = QTableWidgetItem(srv.transport)
        transport_item.setFlags(transport_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.mcp_servers_table.setItem(row, 1, transport_item)

        summary = srv.url if srv.transport != "stdio" else srv.command or ""
        if not summary and srv.transport == "stdio" and srv.args:
            summary = (srv.command or "") + " " + " ".join(srv.args)
        summary_item = QTableWidgetItem(summary or "")
        summary_item.setFlags(summary_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.mcp_servers_table.setItem(row, 2, summary_item)

    # ---- form ↔ row helpers --------------------------------------------

    def _set_mcp_server_form_enabled(self, enabled: bool) -> None:
        for w in (
            self.mcp_name_edit,
            self.mcp_transport_combo,
            self.mcp_command_edit,
            self.mcp_args_edit,
            self.mcp_url_edit,
            self.mcp_env_edit,
            self.mcp_extra_edit,
        ):
            w.setEnabled(enabled)
        if not enabled:
            self.mcp_apply_btn.setEnabled(False)
            self.mcp_revert_btn.setEnabled(False)
        # Transport-specific fields. ``stdio`` → command/args/env;
        # ``sse``/``http`` → url. ``command`` is shown for both so
        # the user can have an HTTP server with a launcher command
        # if they really want one.
        is_stdio = self.mcp_transport_combo.currentText() == "stdio"
        self.mcp_command_edit.setVisible(is_stdio or enabled is False)
        self.mcp_args_edit.setVisible(is_stdio)
        self.mcp_env_edit.setVisible(is_stdio)
        self.mcp_url_edit.setVisible(not is_stdio or enabled is False)

    def _on_mcp_transport_changed(self, _text: str) -> None:
        self._set_mcp_server_form_enabled(
            self._selected_mcp_server_row >= 0
        )
        self._on_mcp_server_form_changed()

    def _on_mcp_server_selection_changed(self) -> None:
        sel = self.mcp_servers_table.selectionModel()
        row = sel.selectedRows()[0].row() if sel and sel.selectedRows() else -1
        self._selected_mcp_server_row = row
        if row < 0:
            self._mcp_server_row_snapshot = None
            self._set_mcp_server_form_enabled(False)
            self.mcp_form_status.setText("Select a row to edit")
            self._refresh_mcp_server_apply_state()
            if hasattr(self, "mcp_remove_btn"):
                self.mcp_remove_btn.setEnabled(False)
            return
        name_item = self.mcp_servers_table.item(row, 0)
        srv = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
        self._set_mcp_server_form_enabled(True)
        self._fill_mcp_server_form(srv)
        self._mcp_server_row_snapshot = self._mcp_server_snapshot_from_form()
        self.mcp_form_status.setText(
            f"Editing row {row + 1} of {self.mcp_servers_table.rowCount()}"
        )
        if hasattr(self, "mcp_remove_btn"):
            self.mcp_remove_btn.setEnabled(True)
        self._refresh_mcp_server_apply_state()

    def _fill_mcp_server_form(self, srv: McpServer | None) -> None:
        """Mirror a server into the form widgets.

        ``None`` produces a blank form (used by Add…). Signal-blocked
        so the populate doesn't re-trigger divergence checks mid-fill.
        """
        widgets = (
            self.mcp_name_edit,
            self.mcp_command_edit,
            self.mcp_args_edit,
            self.mcp_url_edit,
            self.mcp_env_edit,
        )
        for w in widgets:
            w.blockSignals(True)
        self.mcp_transport_combo.blockSignals(True)
        self.mcp_extra_edit.blockSignals(True)
        try:
            if srv is None:
                self.mcp_name_edit.setText("")
                self.mcp_transport_combo.setCurrentText("stdio")
                self.mcp_command_edit.setText("")
                self.mcp_args_edit.setText("")
                self.mcp_url_edit.setText("")
                self.mcp_env_edit.setText("")
                self.mcp_extra_edit.setPlainText("")
            else:
                self.mcp_name_edit.setText(srv.name)
                # Defensive: unknown transports default to stdio.
                transport = srv.transport if srv.transport in _TRANSPORTS else "stdio"
                self.mcp_transport_combo.setCurrentText(transport)
                self.mcp_command_edit.setText(srv.command or "")
                self.mcp_args_edit.setText(" ".join(srv.args))
                self.mcp_url_edit.setText(srv.url or "")
                self.mcp_env_edit.setText(
                    ", ".join(f"{k}={v}" for k, v in srv.env.items())
                )
                if srv.extra:
                    self.mcp_extra_edit.setPlainText(
                        json.dumps(srv.extra, indent=2, sort_keys=True)
                    )
                else:
                    self.mcp_extra_edit.setPlainText("")
        finally:
            for w in widgets:
                w.blockSignals(False)
            self.mcp_transport_combo.blockSignals(False)
            self.mcp_extra_edit.blockSignals(False)
        self._set_mcp_server_form_enabled(True)

    def _parse_env_field(self, text: str) -> dict[str, str]:
        """Parse ``"KEY=VAL, KEY2=VAL2"`` → ``{"KEY": "VAL", ...}``.

        Malformed entries (no ``=``, empty keys) are silently dropped
        — the user will see the parsed count vs the raw text in the
        apply-state hint if any were dropped.
        """
        out: dict[str, str] = {}
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            k, _, v = chunk.partition("=")
            k = k.strip()
            v = v.strip()
            if k:
                out[k] = v
        return out

    def _parse_args_field(self, text: str) -> tuple[str, ...]:
        """Split on whitespace, but respect simple single/double quotes.

        ``"a b" 'c d'`` → ``("a", "b", "c", "d")``. Empty input → ``()``.
        """
        out: list[str] = []
        cur: list[str] = []
        quote: str | None = None
        for ch in text:
            if quote:
                if ch == quote:
                    quote = None
                else:
                    cur.append(ch)
                continue
            if ch in ("'", '"'):
                quote = ch
                continue
            if ch.isspace():
                if cur:
                    out.append("".join(cur))
                    cur = []
                continue
            cur.append(ch)
        if cur:
            out.append("".join(cur))
        return tuple(out)

    def _parse_extra_field(self, text: str) -> dict:
        """Parse the raw-JSON fallback. Empty string → ``{}``.

        Returns ``("error", str)`` on parse failure so the caller
        can show a ``QMessageBox`` and refuse to apply.
        """
        text = text.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return ("error", str(e))
        if not isinstance(parsed, dict):
            return ("error", "must be a JSON object (not a list or scalar)")
        return parsed

    def _mcp_server_snapshot_from_form(self) -> tuple:
        """Capture the form's current values for divergence comparison.

        Returned shape: ``(name, transport, command, args, url, env_text,
        extra_text)``. Plain strings; we don't try to parse env/extra
        here — divergence is on raw text so a re-typing of an
        identical JSON still counts as "no change" if the bytes match.
        """
        return (
            self.mcp_name_edit.text().strip(),
            self.mcp_transport_combo.currentText(),
            self.mcp_command_edit.text(),
            self.mcp_args_edit.text(),
            self.mcp_url_edit.text(),
            self.mcp_env_edit.text(),
            self.mcp_extra_edit.toPlainText(),
        )

    def _on_mcp_server_form_changed(self, *_args: object) -> None:
        if getattr(self, "_selected_mcp_server_row", -1) < 0:
            return
        self._refresh_mcp_server_apply_state()

    def _refresh_mcp_server_apply_state(self) -> None:
        if self._selected_mcp_server_row < 0:
            self.mcp_apply_btn.setEnabled(False)
            self.mcp_revert_btn.setEnabled(False)
            return
        # ``mcp_extra`` parses to ``("error", str)`` on a bad JSON;
        # surface that as a forced Apply-state so the user sees the
        # error and the form doesn't lock them out of fixing it.
        extra = self._parse_extra_field(self.mcp_extra_edit.toPlainText())
        if isinstance(extra, tuple):
            self.mcp_apply_btn.setEnabled(False)
            self.mcp_revert_btn.setEnabled(True)
            self.mcp_form_status.setText(f"Invalid JSON in Extra: {extra[1]}")
            return
        diverges = self._mcp_server_snapshot_from_form() != self._mcp_server_row_snapshot
        self.mcp_apply_btn.setEnabled(diverges)
        self.mcp_revert_btn.setEnabled(diverges)
        if diverges:
            self.mcp_form_status.setText(
                "Edited — click Apply to save, or Revert to discard"
            )
        else:
            self.mcp_form_status.setText(
                f"Row {self._selected_mcp_server_row + 1} — no pending changes"
            )

    def _on_mcp_server_apply(self) -> None:
        """Commit the form's values back to the selected server row.

        Mirrors the API Keys tab's Apply: re-snapshots the form so a
        second Apply without an intervening edit is a no-op, and
        updates the table row's display columns.
        """
        row = self._selected_mcp_server_row
        if row < 0:
            return
        new_name = self.mcp_name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, "Missing name", "Server name cannot be empty.")
            return
        transport = self.mcp_transport_combo.currentText()
        command = self.mcp_command_edit.text().strip() or None
        args = self._parse_args_field(self.mcp_args_edit.text())
        url = self.mcp_url_edit.text().strip() or None
        env = self._parse_env_field(self.mcp_env_edit.text())
        extra_raw = self._parse_extra_field(self.mcp_extra_edit.toPlainText())
        if isinstance(extra_raw, tuple):
            QMessageBox.warning(
                self,
                "Invalid JSON",
                f"The Extra JSON block doesn't parse: {extra_raw[1]}",
            )
            return

        new_srv = McpServer(
            name=new_name,
            transport=transport,
            command=command,
            args=args,
            env=env,
            url=url,
            extra=extra_raw,
        )
        # Update the table row.
        name_item = self.mcp_servers_table.item(row, 0)
        if name_item is not None:
            name_item.setText(new_srv.name)
            name_item.setData(Qt.ItemDataRole.UserRole, new_srv)
        transport_item = self.mcp_servers_table.item(row, 1)
        if transport_item is not None:
            transport_item.setText(new_srv.transport)
        summary_item = self.mcp_servers_table.item(row, 2)
        if summary_item is not None:
            if new_srv.transport != "stdio":
                summary_item.setText(new_srv.url or "")
            else:
                summary = new_srv.command or ""
                if new_srv.args:
                    summary = (summary + " " if summary else "") + " ".join(new_srv.args)
                summary_item.setText(summary)
        self._mcp_server_row_snapshot = self._mcp_server_snapshot_from_form()
        self._refresh_mcp_server_apply_state()

    def _on_mcp_server_revert(self) -> None:
        if self._selected_mcp_server_row < 0:
            return
        name_item = self.mcp_servers_table.item(row := self._selected_mcp_server_row, 0)
        srv = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
        self._fill_mcp_server_form(srv)
        self._mcp_server_row_snapshot = self._mcp_server_snapshot_from_form()
        self._refresh_mcp_server_apply_state()

    def _on_mcp_server_add(self) -> None:
        """Append a blank server row with a unique placeholder name."""
        existing = {
            self.mcp_servers_table.item(r, 0).text()
            for r in range(self.mcp_servers_table.rowCount())
            if self.mcp_servers_table.item(r, 0) is not None
        }
        n = 1
        while f"server-{n}" in existing:
            n += 1
        blank = McpServer(name=f"server-{n}", transport="stdio")
        self._append_mcp_server_row(blank)
        self.mcp_servers_table.selectRow(self.mcp_servers_table.rowCount() - 1)

    def _on_mcp_server_remove(self) -> None:
        row = self._selected_mcp_server_row
        if row < 0:
            return
        self.mcp_servers_table.removeRow(row)
        self._refresh_mcp_server_apply_state()

    # ---- gather on OK ---------------------------------------------------

    def _collect_mcp_servers(self) -> dict[str, McpServer]:
        """Walk the table and build the final ``{name: McpServer}`` map.

        Rows whose ``name`` cell is empty are silently dropped (they
        were never applied). Names with leading/trailing whitespace
        are stripped. Duplicates: the later row wins and a warning is
        surfaced in the dialog's accept flow.
        """
        out: dict[str, McpServer] = {}
        for row in range(self.mcp_servers_table.rowCount()):
            name_item = self.mcp_servers_table.item(row, 0)
            if name_item is None:
                continue
            srv = name_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(srv, McpServer):
                continue
            name = srv.name.strip()
            if not name:
                continue
            out[name] = srv
        return out