"""Read-only code preview panel showing generated Mermaid syntax."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget, QLabel

import re


class MermaidHighlighter(QSyntaxHighlighter):
    """Basic syntax highlighting for Mermaid flowchart text."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._rules: list[tuple[re.Pattern, QTextCharFormat]] = []

        keyword_fmt = QTextCharFormat()
        keyword_fmt.setForeground(QColor("#0000cc"))
        keyword_fmt.setFontWeight(QFont.Bold)
        self._rules.append(
            (re.compile(r"\b(flowchart|graph|subgraph|end)\b"), keyword_fmt)
        )

        direction_fmt = QTextCharFormat()
        direction_fmt.setForeground(QColor("#7b3814"))
        self._rules.append((re.compile(r"\b(TD|TB|BT|RL|LR)\b"), direction_fmt))

        arrow_fmt = QTextCharFormat()
        arrow_fmt.setForeground(QColor("#aa5500"))
        self._rules.append((re.compile(r"(-->|==>|-.->|--o|--x|<-->)"), arrow_fmt))

        label_fmt = QTextCharFormat()
        label_fmt.setForeground(QColor("#008800"))
        self._rules.append((re.compile(r"\|[^|]+\|"), label_fmt))

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class CodePreview(QWidget):
    """Panel showing generated Mermaid code with syntax highlighting."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QLabel(" Mermaid Code")
        header.setStyleSheet(
            "background: #f0f0f0; padding: 4px; font-size: 11px; font-weight: bold;"
        )
        layout.addWidget(header)

        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setFont(QFont("Menlo, Consolas, monospace", 11))
        self._editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self._editor, 1)

        self._highlighter = MermaidHighlighter(self._editor.document())

    def set_mermaid_text(self, text: str) -> None:
        self._editor.setPlainText(text)
