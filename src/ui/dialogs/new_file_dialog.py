"""New File dialog — lets the user pick a file type before creating a tab."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)


_FILE_TYPES = [
    ("Markdown", "markdown", ".md"),
    ("JSON", "json", ".json"),
    ("YAML", "yaml", ".yml"),
    ("HTML", "html", ".html"),
    ("Plain Text", "text", ".txt"),
]


class NewFileDialog(QDialog):
    """Modal dialog offering a choice of file type for a new document."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New File")
        self.setMinimumWidth(300)
        self.setModal(True)

        self._selected_format: str | None = None

        layout = QVBoxLayout(self)

        label = QLabel("Select file type:")
        layout.addWidget(label)

        self._list = QListWidget()
        for display, fmt, ext in _FILE_TYPES:
            item = QListWidgetItem(f"{display}  ({ext})")
            item.setData(Qt.UserRole, fmt)
            self._list.addItem(item)
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_format(self) -> str:
        """Return the format string of the selected file type."""
        item = self._list.currentItem()
        if item is None:
            return "markdown"
        return item.data(Qt.UserRole)

    @staticmethod
    def ask(parent=None) -> str | None:
        """Show the dialog and return the selected format, or None if cancelled."""
        dlg = NewFileDialog(parent)
        if dlg.exec() == QDialog.Accepted:
            return dlg.selected_format()
        return None
