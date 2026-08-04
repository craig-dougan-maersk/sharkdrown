"""Ancillary dialogs used by the profile editor.

- StandardEnvVarsDialog — picker for curated env vars
- _SubagentEditorDialog — small editor for one Subagent
- ManageProfilesDialog — list-and-act for profile collection
"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.env_var_catalog import (
    StandardEnvVar,
    sections as _catalog_sections,
    standard_env_vars as _standard_env_vars,
)
from src.profiles.models import MarketplaceSource, Subagent


# ---- env vars picker -------------------------------------------------------


def _variables_in_section(section: str) -> list[StandardEnvVar]:
    """Subset of :func:`standard_env_vars` whose section matches."""
    return [v for v in _standard_env_vars() if v.section == section]


class StandardEnvVarsDialog(QDialog):
    """Pick one or more curated Claude Code env vars.

    Rendered as a two-column ``QTableView`` — variable name on the
    left, purpose on the right — sectioned by category. Multi-select
    via ctrl/cmd click or shift-click.
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
        self.table.setMinimumHeight(280)
        layout.addWidget(self.table, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _populate(self) -> None:
        from PySide6.QtGui import QBrush, QColor, QStandardItem
        from PySide6.QtCore import Qt as _Qt

        header_brush = QBrush(QColor("#dde4ec"))
        header_flags = _Qt.ItemFlag.ItemIsEnabled
        for section in _catalog_sections():
            name_item = QStandardItem(section)
            name_item.setFlags(header_flags)
            name_item.setBackground(header_brush)
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
                self.model.appendRow([left, right])

    def selected_names(self) -> list[str]:
        """Return the variable names the user picked, in display order."""
        from PySide6.QtCore import Qt as _Qt

        out: list[str] = []
        for index in self.table.selectionModel().selectedRows():
            name_item = self.model.item(index.row(), 0)
            if name_item is None:
                continue
            if name_item.data(_Qt.ItemDataRole.UserRole) == "header":
                continue
            raw = name_item.text().strip()
            if raw.endswith("*"):
                raw = raw[: -len("  *")]
            if raw:
                out.append(raw)
        return out


# ---- subagent editor -------------------------------------------------------


class _SubagentEditorDialog(QDialog):
    """Tiny editor for one :class:`Subagent`.

    Form fields (the same five Claude Code reads as frontmatter):

    - **Name** — ``QLineEdit``; required.
    - **Description** — ``QLineEdit``; required.
    - **Tools** — ``QLineEdit`` (comma-separated); parsed to ``list[str]``.
    - **Model** — ``QComboBox`` populated from the parent profile's
      aliases, with ``"(inherit session default)"`` as the empty
      sentinel. Selecting a real model writes the alias name to the
      subagent's ``model`` field; the proxy's alias-rewrite layer
      translates it back to the real upstream model at request time.
    - **Detail (system prompt)** — ``QPlainTextEdit``; the file body
      after the YAML frontmatter.
    """

    def __init__(
        self,
        subagent: Subagent,
        *,
        model_aliases: tuple[str, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
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

        # Model dropdown. ``"(inherit session default)"`` is the empty
        # sentinel — corresponds to ``model = None`` on the
        # :class:`Subagent`. Aliases are deduplicated + sorted, then
        # any ``(real model: X)`` legacy value is appended last so the
        # user's existing choice round-trips through save.
        from PySide6.QtWidgets import QComboBox

        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        self.model_combo.addItem("(inherit session default)", "")
        for alias in sorted(dict.fromkeys(model_aliases)):
            self.model_combo.addItem(alias, alias)
        # Legacy: if the subagent has a model that isn't a known
        # alias (e.g. a real upstream ID stored by an older version),
        # surface it as a labeled last-resort entry.
        existing_model = self._sa.model
        if existing_model:
            match = False
            for i in range(self.model_combo.count()):
                if self.model_combo.itemData(i) == existing_model:
                    self.model_combo.setCurrentIndex(i)
                    match = True
                    break
            if not match:
                self.model_combo.addItem(
                    f"(real model: {existing_model})", existing_model
                )
                self.model_combo.setCurrentIndex(self.model_combo.count() - 1)
        layout.addRow("Model:", self.model_combo)

        self.prompt_edit = QPlainTextEdit(self._sa.system_prompt)
        self.prompt_edit.setPlaceholderText("System prompt body…")
        layout.addRow(self.prompt_edit)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
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
        model_value = self.model_combo.currentData()
        model = (
            str(model_value).strip() or None
            if model_value is not None
            else None
        )
        return Subagent(
            name=name,
            description=self.desc_edit.text(),
            system_prompt=self.prompt_edit.toPlainText(),
            tools=tools,
            model=model,
        )


# ---- marketplace editor ----------------------------------------------------


class _MarketplaceEditorDialog(QDialog):
    """Editor for one :class:`MarketplaceSource`.

    Mirrors the four ``source`` kinds Claude Code accepts
    (``github``, ``git``, ``url``, ``git-subdir``) by showing only
    the sub-fields relevant to the chosen kind. Validation on accept
    refuses to OK if the kind's required field is empty.
    """

    def __init__(
        self,
        name: str,
        source: MarketplaceSource | None = None,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Marketplace — {name or 'New'}")
        self.resize(560, 360)
        self._initial_name = name
        layout = QFormLayout(self)

        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("e.g. claude-plugins-official")
        layout.addRow("Marketplace name:", self.name_edit)

        self.source_kind_combo = QComboBox()
        for kind in ("github", "git", "url", "git-subdir"):
            self.source_kind_combo.addItem(kind)
        # Default new entries to github — it's the most common kind
        # and matches the pattern in ``known_marketplaces.json`` we
        # see on real installs.
        self.source_kind_combo.setCurrentText(
            source.source if source else "github"
        )
        self.source_kind_combo.currentTextChanged.connect(
            self._on_kind_changed
        )
        layout.addRow("Source kind:", self.source_kind_combo)

        self.repo_edit = QLineEdit(source.repo if source else "")
        self.repo_edit.setPlaceholderText("owner/repo, e.g. anthropics/claude-plugins-official")
        layout.addRow("Repo (github):", self.repo_edit)

        self.url_edit = QLineEdit(source.url if source else "")
        self.url_edit.setPlaceholderText("https://example.com/repo.git")
        layout.addRow("URL (git / url / git-subdir):", self.url_edit)

        self.path_edit = QLineEdit(source.path if source else "")
        self.path_edit.setPlaceholderText("plugins/awesome")
        layout.addRow("Subdir path (git-subdir):", self.path_edit)

        self.ref_edit = QLineEdit(source.ref if source else "")
        self.ref_edit.setPlaceholderText("main, v1.2.3, …")
        layout.addRow("Ref (optional):", self.ref_edit)

        self.sha_edit = QLineEdit(source.sha if source else "")
        self.sha_edit.setPlaceholderText("40-char commit SHA")
        layout.addRow("SHA (optional):", self.sha_edit)

        self.install_location_edit = QLineEdit(
            source.install_location if source and source.install_location else ""
        )
        self.install_location_edit.setPlaceholderText(
            "(auto — ~/.claude/plugins/marketplaces/<name>)"
        )
        layout.addRow("Install location (optional):", self.install_location_edit)

        self._on_kind_changed(self.source_kind_combo.currentText())

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addRow(self.button_box)

    def _on_kind_changed(self, kind: str) -> None:
        """Show only the sub-fields relevant to the chosen source kind.

        ``github`` → repo. ``git`` / ``url`` → url. ``git-subdir`` →
        url + path. ``ref`` + ``sha`` are always visible (they apply
        to all kinds). ``install_location`` is always visible.
        """
        self.repo_edit.setVisible(kind == "github")
        self.url_edit.setVisible(kind in ("git", "url", "git-subdir"))
        self.path_edit.setVisible(kind == "git-subdir")

    def _on_accept(self) -> None:
        kind = self.source_kind_combo.currentText()
        if kind == "github":
            if not self.repo_edit.text().strip():
                QMessageBox.warning(
                    self,
                    "Missing repo",
                    "GitHub marketplaces require a 'owner/repo' in the Repo field.",
                )
                return
        elif kind in ("git", "url", "git-subdir"):
            if not self.url_edit.text().strip():
                QMessageBox.warning(
                    self,
                    "Missing URL",
                    f"{kind} marketplaces require a URL.",
                )
                return
        if not self.name_edit.text().strip():
            QMessageBox.warning(
                self,
                "Missing name",
                "Marketplace name cannot be empty.",
            )
            return
        self.accept()

    def to_source(self) -> tuple[str, MarketplaceSource]:
        """Return ``(name, source)`` ready for insertion into the profile.

        Empty optional fields become ``None`` so the serialiser drops
        them from the on-disk JSON.
        """
        name = self.name_edit.text().strip()
        kind = self.source_kind_combo.currentText()
        src = MarketplaceSource(
            source=kind,
            repo=(self.repo_edit.text().strip() or None)
            if kind == "github" else None,
            url=(self.url_edit.text().strip() or None)
            if kind in ("git", "url", "git-subdir") else None,
            path=(self.path_edit.text().strip() or None)
            if kind == "git-subdir" else None,
            ref=(self.ref_edit.text().strip() or None),
            sha=(self.sha_edit.text().strip() or None),
            install_location=(self.install_location_edit.text().strip() or None),
        )
        return name, src


# ---- manage profiles dialog ------------------------------------------------


class ManageProfilesDialog(QDialog):
    """List-and-act dialog for the user's profile collection.

    The Default profile is pinned at index 0 and cannot be deleted.
    Add/Edit/Delete buttons operate on the selected profile.
    """

    def __init__(
        self,
        profile_store,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
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

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        close_container = QWidget()
        close_container.setLayout(close_row)
        layout.addWidget(close_container)

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for p in self._store.load():
            label = p.name
            if getattr(p, "is_default", False) or p.name == self._default_name:
                label = f"{p.name}  (default)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.list_widget.addItem(item)
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
        is_default = False
        if has_sel:
            p = item.data(Qt.ItemDataRole.UserRole)
            is_default = (
                p is not None
                and (getattr(p, "is_default", False) or p.name == self._default_name)
            )
        self.delete_btn.setEnabled(has_sel and not is_default)

    def _on_add(self) -> None:
        default = self._store.ensure_default()
        draft = copy.deepcopy(default)
        draft.name = ""
        draft.slug = ""
        draft.is_default = False
        self._open_editor_for(draft, after_save=lambda new_p: self._on_added(new_p))

    def _on_edit(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        p = item.data(Qt.ItemDataRole.UserRole)
        if p is None:
            return
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
        from src.ui.claude.profile_editor.dialog import ProfileEditorDialog
        from src.profiles.global_settings import GlobalSettingsStore
        from src.proxy.paths import data_dir

        # Inject the on-disk global settings store so the profile
        # editor's Model Configuration tab can populate its
        # Provider combo from Claude > Settings > Providers — the
        # Manage Profiles dialog doesn't have a live loader
        # installed, but the on-disk fallback in
        # ``_global_settings_for_validation`` does the right thing
        # when the user has saved Claude > Settings at least
        # once.
        gs_store = GlobalSettingsStore(data_dir() / "claude_global.json")
        dlg = ProfileEditorDialog(
            draft,
            credential_store=self._store.credential_store,
            global_settings_store=gs_store,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        edited = dlg.to_profile()
        if not edited.name.strip():
            return
        if edited.name == self._default_name:
            edited.is_default = True
        self._store.upsert(edited)
        after_save(edited)
        self._refresh_list()

    def _on_added(self, new_profile) -> None:
        pass

    def _on_edited(self, edited_profile) -> None:
        pass

    def _on_delete(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        p = item.data(Qt.ItemDataRole.UserRole)
        if p is None:
            return
        if p.name == self._default_name or getattr(p, "is_default", False):
            return
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
