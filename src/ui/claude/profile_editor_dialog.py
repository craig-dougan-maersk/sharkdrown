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
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.credential_store import CredentialStore
from src.profiles.models import ClaudeProfile, Subagent, slugify


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
        # Track whether the credential was edited during this session
        # so we know whether to write on OK.
        self._cred_edited = False
        self._cred_existing_value = (
            self._creds.get(self._profile.api_key_ref)
            if self._profile.api_key_ref
            else None
        )
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

        # ---- General form ------------------------------------------------
        general_box = QWidget()
        general_layout = QFormLayout(general_box)
        general_layout.setContentsMargins(0, 0, 0, 0)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Default")
        general_layout.addRow("Name:", self.name_edit)

        self.default_model_edit = QLineEdit()
        self.default_model_edit.setPlaceholderText("model name from the table below")
        general_layout.addRow("Default model:", self.default_model_edit)

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
        layout.addWidget(general_box)

        # ---- Model Endpoints table ---------------------------------------
        layout.addWidget(self._section_label("Model Endpoints"))
        ep_help = QLabel(
            "Map model names (e.g. <code>claude-opus-4-8</code>) to upstream URLs. "
            "Models not listed fall through to the proxy's <em>default endpoint</em> "
            "if one is configured in the proxy settings."
        )
        ep_help.setWordWrap(True)
        ep_help.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(ep_help)

        self.endpoints_table = QTableWidget(0, 2)
        self.endpoints_table.setHorizontalHeaderLabels(["Model", "Endpoint URL"])
        self.endpoints_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.endpoints_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.endpoints_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.endpoints_table)
        ep_btns = QHBoxLayout()
        ep_add = QPushButton("Add…")
        ep_add.clicked.connect(lambda: self._append_table_row(self.endpoints_table, "", ""))
        ep_remove = QPushButton("Remove Selected")
        ep_remove.clicked.connect(
            lambda: self._remove_selected_rows(self.endpoints_table)
        )
        ep_btns.addWidget(ep_add)
        ep_btns.addWidget(ep_remove)
        ep_btns.addStretch(1)
        ep_btn_container = QWidget()
        ep_btn_container.setLayout(ep_btns)
        layout.addWidget(ep_btn_container)

        # ---- API Key -----------------------------------------------------
        layout.addWidget(self._section_label("API Key"))
        cred_help = QLabel(
            "The Claude CLI sends this key on every request. The proxy "
            "currently forwards it verbatim, so a working key is required "
            "for Claude Code to function. Stored in <code>credentials.json</code> "
            "with 0600 permissions."
        )
        cred_help.setWordWrap(True)
        cred_help.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(cred_help)

        cred_row = QFormLayout()
        cred_row.setContentsMargins(0, 0, 0, 0)
        self.cred_ref_edit = QLineEdit()
        self.cred_ref_edit.setPlaceholderText("default-key")
        cred_row.addRow("Reference:", self.cred_ref_edit)
        self.cred_value_edit = QLineEdit()
        self.cred_value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.cred_value_edit.setPlaceholderText("(leave blank to keep existing value)")
        self.cred_value_edit.textChanged.connect(self._on_cred_value_changed)
        cred_row.addRow("Value:", self.cred_value_edit)
        cred_container = QWidget()
        cred_container.setLayout(cred_row)
        layout.addWidget(cred_container)

        # ---- Env Vars ----------------------------------------------------
        layout.addWidget(self._section_label("Environment Variables"))
        env_help = QLabel(
            "Extra environment variables exported to <code>claude</code> for "
            "sessions of this profile. <em>ANTHROPIC_BASE_URL</em>, "
            "<em>CLAUDE_CONFIG_DIR</em> and <em>ANTHROPIC_API_KEY</em> are "
            "owned by the integration and cannot be overridden here."
        )
        env_help.setWordWrap(True)
        env_help.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(env_help)

        self.env_table = QTableWidget(0, 2)
        self.env_table.setHorizontalHeaderLabels(["Name", "Value"])
        self.env_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.env_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.env_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.env_table)
        env_btns = QHBoxLayout()
        env_add = QPushButton("Add…")
        env_add.clicked.connect(lambda: self._append_table_row(self.env_table, "", ""))
        env_remove = QPushButton("Remove Selected")
        env_remove.clicked.connect(lambda: self._remove_selected_rows(self.env_table))
        env_btns.addWidget(env_add)
        env_btns.addWidget(env_remove)
        env_btns.addStretch(1)
        env_btn_container = QWidget()
        env_btn_container.setLayout(env_btns)
        layout.addWidget(env_btn_container)

        # ---- Claude Settings (raw JSON) ---------------------------------
        layout.addWidget(self._section_label("Claude Settings (raw JSON)"))
        settings_help = QLabel(
            "Free-form <code>settings.json</code> keys (permissions, hooks, "
            "etc.). <em>model</em> and <em>availableModels</em> are set "
            "automatically from the fields above and override anything "
            "here."
        )
        settings_help.setWordWrap(True)
        settings_help.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(settings_help)

        self.settings_edit = QPlainTextEdit()
        mono = QPlainTextEdit.font()
        from PySide6.QtGui import QFontDatabase

        mono.setFamily(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family())
        self.settings_edit.setFont(mono)
        self.settings_edit.setPlaceholderText('{\n  "permissions": {}\n}')
        layout.addWidget(self.settings_edit, 1)

        # ---- Subagents ---------------------------------------------------
        layout.addWidget(self._section_label("Subagents"))
        sub_help = QLabel(
            "Each subagent becomes a <code>&lt;config&gt;/agents/&lt;name&gt;.md</code> "
            "file with YAML frontmatter (Claude Code's native format)."
        )
        sub_help.setWordWrap(True)
        sub_help.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(sub_help)

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
        layout.addWidget(sub_container)

        # ---- OK / Cancel -------------------------------------------------
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        lbl.setStyleSheet("margin-top: 8px;")
        return lbl

    def _populate(self) -> None:
        self.name_edit.setText(self._profile.name)
        self.default_model_edit.setText(self._profile.default_model or "")
        self.cwd_edit.setText(self._profile.session_cwd or "")
        _populate_pairs(
            self.endpoints_table,
            list(self._profile.model_endpoints.items()),
        )
        # Ensure at least one row so the user has somewhere to type.
        if self.endpoints_table.rowCount() == 0:
            self._append_table_row(self.endpoints_table, "", "")
        self.cred_ref_edit.setText(self._profile.api_key_ref or "default-key")
        # Don't pre-fill the credential value — leave blank to keep
        # what's already on disk.
        self._cred_value_edit.setText("")
        _populate_pairs(self.env_table, list(self._profile.env_vars.items()))
        try:
            settings_text = json.dumps(
                self._profile.claude_settings, indent=2, sort_keys=True
            )
        except (TypeError, ValueError):
            settings_text = ""
        self.settings_edit.setPlainText(settings_text)
        self._refresh_subagent_list()

    def _refresh_subagent_list(self) -> None:
        self.subagent_list.clear()
        for sa in self._profile.subagents:
            item = QListWidgetItem(sa.name)
            item.setData(Qt.ItemDataRole.UserRole, sa)
            self.subagent_list.addItem(item)

    def _append_table_row(self, table: QTableWidget, k: str, v: str) -> None:
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(k))
        table.setItem(row, 1, QTableWidgetItem(v))

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
        self._cred_edited = True

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
        # ---- validate name ---------------------------------------------
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Profile name cannot be empty.")
            return
        self._profile.name = name
        self._profile.slug = slugify(name)

        # ---- model_endpoints -------------------------------------------
        endpoints = dict(_table_pairs(self.endpoints_table))
        if not endpoints:
            QMessageBox.warning(
                self,
                "No model endpoints",
                "Add at least one model endpoint so the proxy knows where to route.",
            )
            return
        self._profile.model_endpoints = endpoints

        # ---- default_model validation ----------------------------------
        default_model = self.default_model_edit.text().strip() or None
        if default_model and default_model not in endpoints:
            QMessageBox.warning(
                self,
                "Unknown default model",
                f"Default model {default_model!r} is not in the model endpoints table.",
            )
            return
        self._profile.default_model = default_model

        # ---- session_cwd ------------------------------------------------
        cwd = self.cwd_edit.text().strip() or None
        self._profile.session_cwd = cwd

        # ---- credential ref + value ------------------------------------
        cred_ref = self.cred_ref_edit.text().strip() or None
        self._profile.api_key_ref = cred_ref
        if self._cred_edited and cred_ref:
            new_value = self._cred_value_edit.text()
            if new_value == "":
                # Blank edit = "clear the credential". Only honour
                # this if the credential previously existed.
                if self._cred_existing_value is not None:
                    self._creds.delete(cred_ref)
            else:
                self._creds.set(cred_ref, new_value)

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

    # ---- public API --------------------------------------------------------

    def to_profile(self) -> ClaudeProfile:
        """Return the edited profile (caller's responsibility to store it)."""
        return self._profile


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