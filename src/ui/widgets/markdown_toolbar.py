"""Markdown formatting toolbar for the Documents editor."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QTextEdit,
    QToolBar,
)
from PySide6.QtGui import QTextCursor


class MarkdownToolbar(QToolBar):
    """Toolbar providing quick markdown formatting actions.

    Operates on whichever QTextEdit is set via ``set_editor()``.
    """

    def __init__(self, parent=None) -> None:
        super().__init__("Markdown", parent)
        self._editor: QTextEdit | None = None
        self._build_actions()

    def set_editor(self, editor: QTextEdit | None) -> None:
        self._editor = editor

    def _build_actions(self) -> None:
        self.addAction("H1", lambda: self._prefix_line("# "))
        self.addAction("H2", lambda: self._prefix_line("## "))
        self.addAction("H3", lambda: self._prefix_line("### "))
        self.addSeparator()
        self.addAction("B", lambda: self._wrap("**", "**"))
        self.addAction("I", lambda: self._wrap("*", "*"))
        self.addSeparator()
        self.addAction("• List", lambda: self._prefix_line("- "))
        self.addAction("1. List", lambda: self._prefix_line("1. "))
        self.addSeparator()
        self.addAction("Link", self._insert_link)
        self.addAction("Table", self._insert_table)

    def _cursor(self) -> QTextCursor | None:
        if self._editor is None:
            return None
        return self._editor.textCursor()

    def _prefix_line(self, prefix: str) -> None:
        if self._editor is None:
            return
        cursor = self._editor.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        cursor.insertText(prefix)
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()

    def _wrap(self, before: str, after: str) -> None:
        if self._editor is None:
            return
        cursor = self._editor.textCursor()
        selected = cursor.selectedText()
        if selected:
            cursor.insertText(f"{before}{selected}{after}")
        else:
            pos = cursor.position()
            cursor.insertText(f"{before}{after}")
            cursor.setPosition(pos + len(before))
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()

    def _insert_link(self) -> None:
        if self._editor is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Insert Link")
        form = QFormLayout(dlg)
        text_input = QLineEdit()
        url_input = QLineEdit()
        text_input.setPlaceholderText("Link text")
        url_input.setPlaceholderText("https://")

        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            text_input.setText(cursor.selectedText())

        form.addRow("Text:", text_input)
        form.addRow("URL:", url_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec() == QDialog.Accepted:
            text = text_input.text() or "link"
            url = url_input.text() or "url"
            cursor.insertText(f"[{text}]({url})")
            self._editor.setTextCursor(cursor)
        self._editor.setFocus()

    def _insert_table(self) -> None:
        if self._editor is None:
            return
        table = (
            "| Column 1 | Column 2 | Column 3 |\n"
            "|----------|----------|----------|\n"
            "|          |          |          |\n"
            "|          |          |          |\n"
        )
        cursor = self._editor.textCursor()
        cursor.insertText(table)
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()
