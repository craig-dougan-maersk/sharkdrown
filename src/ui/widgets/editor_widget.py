"""
Editor widget for text editing with syntax highlighting.

This module provides a text editor widget with:
- Syntax highlighting for Markdown, JSON, and YAML
- Text change signals
"""

from PySide6.QtCore import Signal
from PySide6.QtGui import (
    QTextCharFormat,
    QColor,
    QFont,
    QSyntaxHighlighter,
    QPainter,
)
from PySide6.QtWidgets import (
    QTextEdit,
    QWidget,
    QVBoxLayout,
)

from src.core.document import DocumentTab


class MarkdownHighlighter(QSyntaxHighlighter):
    """Syntax highlighter for Markdown files."""

    def __init__(self, document):
        super().__init__(document)
        self._formats = self._create_formats()

    def _create_formats(self) -> dict:
        """Create text formats for different syntax elements."""
        formats = {}

        # Headers
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold)
        fmt.setForeground(QColor("#005cc5"))
        formats["header"] = fmt

        # Code blocks
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#f6f8fa"))
        fmt.setForeground(QColor("#24292e"))
        formats["code"] = fmt

        # Inline code
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#f6f8fa"))
        fmt.setForeground(QColor("#d73a49"))
        formats["inline_code"] = fmt

        # Links
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#03267f"))
        fmt.setUnderlineStyle(QTextCharFormat.SingleUnderline)
        formats["link"] = fmt

        # Bold
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold)
        fmt.setForeground(QColor("#24292e"))
        formats["bold"] = fmt

        # Italic
        fmt = QTextCharFormat()
        fmt.setFontItalic(True)
        fmt.setForeground(QColor("#24292e"))
        formats["italic"] = fmt

        return formats

    def highlightBlock(self, text: str) -> None:
        """Highlight the given text block."""
        # Headers (# header)
        if text.startswith("#"):
            length = 0
            for char in text:
                if char == "#":
                    length += 1
                else:
                    break
            self.setFormat(0, length, self._formats["header"])

        # Code blocks (``` or 4 spaces)
        if "```" in text or text.startswith("    "):
            self.setFormat(0, len(text), self._formats["code"])

        # Inline code (`code`)
        import re
        matches = re.finditer(r"`[^`]+`", text)
        for match in matches:
            self.setFormat(match.start(), match.end() - match.start(), self._formats["inline_code"])

        # Bold (**bold**)
        matches = re.finditer(r"\*\*[^*]+\*\*", text)
        for match in matches:
            self.setFormat(match.start(), match.end() - match.start(), self._formats["bold"])

        # Italic (*italic*)
        matches = re.finditer(r"\*[^*]+\*", text)
        for match in matches:
            # Skip if it's part of bold
            if not re.match(r"\*\*", text[match.start() - 1 : match.start()]):
                self.setFormat(match.start(), match.end() - match.start(), self._formats["italic"])

        # Links [text](url)
        matches = re.finditer(r"\[[^\]]+\]\([^\)]+\)", text)
        for match in matches:
            self.setFormat(match.start(), match.end() - match.start(), self._formats["link"])


class JSONHighlighter(QSyntaxHighlighter):
    """Syntax highlighter for JSON files."""

    def __init__(self, document):
        super().__init__(document)
        self._formats = self._create_formats()

    def _create_formats(self) -> dict:
        """Create text formats for JSON syntax elements."""
        formats = {}

        # Strings
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#03267f"))
        formats["string"] = fmt

        # Numbers
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#005cc5"))
        formats["number"] = fmt

        # Keywords (true, false, null)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#d73a49"))
        formats["keyword"] = fmt

        # Braces
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#24292e"))
        formats["brace"] = fmt

        return formats

    def highlightBlock(self, text: str) -> None:
        """Highlight the given text block."""
        import re

        # Strings
        matches = re.finditer(r'"[^"\\]*(\\.[^"\\]*)*"', text)
        for match in matches:
            self.setFormat(match.start(), match.end() - match.start(), self._formats["string"])

        # Numbers
        matches = re.finditer(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", text)
        for match in matches:
            self.setFormat(match.start(), match.end() - match.start(), self._formats["number"])

        # Keywords
        for keyword in ["true", "false", "null"]:
            pattern = rf"\b{keyword}\b"
            matches = re.finditer(pattern, text)
            for match in matches:
                self.setFormat(match.start(), match.end() - match.start(), self._formats["keyword"])

        # Braces
        for char in ["{", "}", "[", "]"]:
            for i, c in enumerate(text):
                if c == char:
                    self.setFormat(i, 1, self._formats["brace"])


class YAMLHighlighter(QSyntaxHighlighter):
    """Syntax highlighter for YAML files."""

    def __init__(self, document):
        super().__init__(document)
        self._formats = self._create_formats()

    def _create_formats(self) -> dict:
        """Create text formats for YAML syntax elements."""
        formats = {}

        # Keys
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#03267f"))
        fmt.setFontWeight(QFont.Bold)
        formats["key"] = fmt

        # Strings
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#24292e"))
        formats["string"] = fmt

        # Numbers
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#005cc5"))
        formats["number"] = fmt

        # Boolean
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#d73a49"))
        formats["boolean"] = fmt

        return formats

    def highlightBlock(self, text: str) -> None:
        """Highlight the given text block."""
        import re

        # Keys (before colon)
        matches = re.finditer(r"^(\s*)([^:#\s]+)\s*:", text)
        for match in matches:
            self.setFormat(match.start(2), match.end(2) - match.start(2), self._formats["key"])

        # Numbers
        matches = re.finditer(r"\b-?\d+(\.\d+)?\b", text)
        for match in matches:
            self.setFormat(match.start(), match.end() - match.start(), self._formats["number"])

        # Booleans
        for keyword in ["true", "false", "True", "False"]:
            pattern = rf"\b{keyword}\b"
            matches = re.finditer(pattern, text)
            for match in matches:
                self.setFormat(match.start(), match.end() - match.start(), self._formats["boolean"])


class EditorWidget(QWidget):
    """Editor widget with syntax highlighting and line numbers."""

    # Coarse "anything changed" signal. Fine for tab-title refresh.
    text_changed = Signal()
    # Carries the new text so listeners (preview, save-status) can react
    # without re-querying the editor.
    content_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()
        self._highlighter = None
        self._document: DocumentTab | None = None

    def _init_ui(self) -> None:
        """Initialize the UI components."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Text editor
        self.editor = QTextEdit()
        self.editor.textChanged.connect(self._on_text_changed)

        layout.addWidget(self.editor)

    def _on_text_changed(self) -> None:
        """Handle text change.

        Writes the new text back onto the bound DocumentTab and notifies
        listeners. Both the preview and ``is_modified()`` depend on
        ``DocumentTab.content`` being current, so this is the single place
        the write happens.
        """
        text = self.editor.toPlainText()
        if self._document is not None:
            self._document.content = text
        self.text_changed.emit()
        self.content_changed.emit(text)

    @property
    def document(self) -> DocumentTab | None:
        """Get the current document."""
        return self._document

    @document.setter
    def document(self, value: DocumentTab) -> None:
        """Set the document and update highlighter."""
        self._document = value
        self.editor.setPlainText(value.content)
        self._update_highlighter()

    def _update_highlighter(self) -> None:
        """Update syntax highlighter based on document type."""
        if self._highlighter:
            self._highlighter.setDocument(None)

        doc = self.document
        if doc is None:
            return

        if doc.format_type == "markdown":
            self._highlighter = MarkdownHighlighter(self.editor.document())
        elif doc.format_type == "json":
            self._highlighter = JSONHighlighter(self.editor.document())
        elif doc.format_type == "yaml":
            self._highlighter = YAMLHighlighter(self.editor.document())

        self.editor.document().modificationChanged.connect(
            lambda: self.text_changed.emit()
        )

    def toPlainText(self) -> str:
        """Get the plain text content."""
        return self.editor.toPlainText()

    def setPlainText(self, text: str) -> None:
        """Set the plain text content."""
        self.editor.setPlainText(text)