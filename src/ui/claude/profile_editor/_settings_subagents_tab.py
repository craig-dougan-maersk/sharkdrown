from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.models import Subagent
from src.ui.claude.profile_editor._dialogs import _SubagentEditorDialog


class SettingsSubagentsTabMixin:
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

    def _refresh_subagent_list(self) -> None:
        self.subagent_list.clear()
        for sa in self._profile.subagents:
            item = QListWidgetItem(sa.name)
            item.setData(Qt.ItemDataRole.UserRole, sa)
            self.subagent_list.addItem(item)

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

    def _current_model_aliases(self) -> tuple[str, ...]:
        """Union of every alias across the profile's endpoints.

        Drives the model dropdown in ``_SubagentEditorDialog`` —
        subagents typically pick an alias (not a real upstream ID)
        so the proxy can route them like any other request.
        """
        out: list[str] = []
        for endpoint in self._profile.model_endpoints.values():
            if endpoint is None:
                continue
            for alias in (getattr(endpoint, "aliases", ()) or ()):
                if isinstance(alias, str) and alias.strip():
                    out.append(alias)
        return tuple(dict.fromkeys(out))

    def _edit_subagent_in_place(self, sa: Subagent) -> bool:
        dlg = _SubagentEditorDialog(
            sa,
            model_aliases=self._current_model_aliases(),
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            updated = dlg.to_subagent()
            sa.name = updated.name
            sa.description = updated.description
            sa.system_prompt = updated.system_prompt
            sa.tools = updated.tools
            sa.model = updated.model
            return True
        return False
