"""
Editor widget for text editing with syntax highlighting.

This module provides a text editor widget with:
- Syntax highlighting for Markdown, JSON, and YAML
- Text change signals
"""

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QTextCharFormat,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QSyntaxHighlighter,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QTextEdit,
    QWidget,
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


class _LineNumberArea(QWidget):
    """Narrow gutter that paints 1-based line numbers next to the editor.

    Lives in a horizontal layout next to the ``QTextEdit`` and tracks
    its vertical scroll position + viewport height. The QTextEdit's
    existing vertical scrollbar remains the source of truth for what
    the user sees; this widget just paints numbers in the gutter.

    A bare ``QWidget`` is cheaper than a ``QPlainTextEdit`` configured
    to behave as one, and lets us paint exactly the same colour/alignment
    we want without inheriting selection logic we'd then have to suppress.
    """

    def __init__(self, editor: "EditorWidget", parent=None):
        super().__init__(parent)
        self._editor_widget = editor
        # No background by default — paint the gutter ourselves so the
        # colour matches the rest of the document tab strip.
        self.setAttribute(Qt.WA_StyledBackground, False)

    def sizeHint(self) -> QSize:
        # Hide entirely when the editor's line numbers are off.
        if not self._editor_widget.line_numbers_enabled:
            return QSize(0, 0)
        digits = max(2, len(str(max(1, self._editor_widget.editor.document().blockCount()))))
        # ~10px per digit + a small right padding; matches QPlainTextEdit's
        # default gutter width closely enough that the text doesn't reflow.
        fm = QFontMetrics(self._editor_widget.editor.font())
        return QSize(fm.horizontalAdvance("9") * digits + 8, 0)

    def paintEvent(self, event) -> None:  # noqa: D401
        if not self._editor_widget.line_numbers_enabled:
            return
        editor = self._editor_widget.editor

        # QTextEdit exposes block geometry via the document layout rather
        # than QPlainTextEdit's firstVisibleBlock()/contentOffset()/
        # blockBoundingGeometry() API.
        layout = editor.document().documentLayout()
        scroll_y = editor.verticalScrollBar().value()

        # Find the first block whose bottom edge is at or below the viewport.
        block = editor.document().begin()
        if not block.isValid():
            return

        gutter_bg = self.palette().alternateBase().color()
        gutter_fg = self.palette().mid().color()

        painter = QPainter(self)
        painter.fillRect(event.rect(), gutter_bg)
        painter.setPen(gutter_fg)
        painter.setFont(editor.font())

        height = editor.fontMetrics().height()
        event_bottom = event.rect().bottom()
        event_top = event.rect().top()

        while block.isValid():
            block_rect = layout.blockBoundingRect(block)
            block_top = int(block_rect.y()) - scroll_y
            block_bottom = block_top + int(block_rect.height())

            if block_top > event_bottom:
                break
            if block_bottom >= event_top:
                rect = QRect(0, block_top, self.width() - 4, height)
                painter.drawText(
                    rect,
                    int(Qt.AlignRight | Qt.AlignVCenter),
                    str(block.blockNumber() + 1),
                )
            block = block.next()

        painter.end()


class EditorWidget(QWidget):
    """Editor widget with syntax highlighting and line numbers."""

    # Coarse "anything changed" signal. Fine for tab-title refresh.
    text_changed = Signal()
    # Carries the new text so listeners (preview, save-status) can react
    # without re-querying the editor.
    content_changed = Signal(str)

    DEFAULT_LINE_NUMBERS_ENABLED = True

    def __init__(self, parent=None):
        super().__init__(parent)
        # ``_line_numbers_enabled`` must be initialised before
        # ``_init_ui`` because the latter calls the width update once
        # at construction; the early call would otherwise hit an
        # AttributeError on the not-yet-set flag.
        self._line_numbers_enabled = self.DEFAULT_LINE_NUMBERS_ENABLED
        self._init_ui()
        self._highlighter = None
        self._document: DocumentTab | None = None

    def _init_ui(self) -> None:
        """Initialize the UI components.

        Layout is ``QHBoxLayout`` (not ``QVBoxLayout`` as before) so the
        ``_LineNumberArea`` gutter can sit beside the editor. The old
        layout had only one child and was functionally identical to a
        no-op, so swapping ``QVBoxLayout`` → ``QHBoxLayout`` is safe.
        """
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Text editor
        self.editor = QTextEdit()
        self.editor.textChanged.connect(self._on_text_changed)

        # Gutter painted to the left of the editor. Created with ``self``
        # as parent so it's destroyed alongside the EditorWidget.
        self._line_number_area = _LineNumberArea(self, parent=self)

        layout.addWidget(self._line_number_area)
        layout.addWidget(self.editor)

        # Repaint the gutter whenever the editor scrolls, resizes, or
        # its content changes. ``QTextEdit`` (unlike ``QPlainTextEdit``)
        # has no ``blockCountChanged``/``updateRequest``/``fontChanged``
        # signals, so we listen to ``textChanged`` (covers
        # inserts/deletes) and to the vertical scrollbar's
        # ``valueChanged`` (covers scroll). Font changes are routed
        # through ``adjust_font_size``/``reset_font_size`` which call
        # ``_on_editor_font_changed`` directly — that's the only path
        # by which the editor's font can change.
        self.editor.textChanged.connect(self._on_editor_text_changed_for_gutter)
        self.editor.verticalScrollBar().valueChanged.connect(
            self._on_editor_scrolled
        )
        # Seed the initial width for the (default-on) line-number gutter.
        # ``blockCount()`` lives on ``QPlainTextEdit`` only; on
        # ``QTextEdit`` we go through ``document().blockCount()``.
        self._update_line_number_area_width(self.editor.document().blockCount())
        # ``cursorPositionChanged`` already drives the status bar's
        # ``Ln X, Col Y`` label, so wiring it again here would be
        # redundant for repaints; the editor viewport already updates
        # the gutter on scroll via ``updateRequest``.

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

    # ---- line numbers -----------------------------------------------------

    @property
    def line_numbers_enabled(self) -> bool:
        """Whether the gutter is currently visible."""
        return self._line_numbers_enabled

    def set_line_numbers_enabled(self, enabled: bool) -> None:
        """Show or hide the line-number gutter.

        ``enabled=True`` makes the gutter visible; ``False`` collapses
        it to zero width. Persisting across restarts is the caller's
        job (see ``_menu_construction.py``), so the widget stays
        stateless across ``MainWindow`` sessions.
        """
        enabled = bool(enabled)
        if enabled == self._line_numbers_enabled:
            return
        self._line_numbers_enabled = enabled
        # ``setVisible(False)`` collapses the gutter without affecting
        # the editor's geometry — the editor will expand to fill the
        # freed space and the gutter's ``sizeHint`` returns ``(0, 0)``.
        self._line_number_area.setVisible(enabled)
        # Repaint because hiding changes the gutter's geometry and the
        # editor's contents offset.
        self._line_number_area.update()

    def _update_line_number_area_width(self, _new_block_count: int) -> None:
        """Recompute gutter width to fit the largest line number.

        ``QTextEdit`` has no ``blockCountChanged`` signal so this is
        driven from ``_on_editor_text_changed_for_gutter`` (which
        passes the current ``blockCount()``) and also called once at
        construction with the initial block count. Cheap because
        ``QTextDocument.blockCount()`` is just an integer getter.
        """
        if not self._line_numbers_enabled:
            return
        self._line_number_area.updateGeometry()

    def _on_editor_text_changed_for_gutter(self) -> None:
        """Bridge from ``textChanged`` to the width-update callback."""
        if not self._line_numbers_enabled:
            return
        # ``QTextEdit.blockCount()`` doesn't exist — the count lives on
        # the underlying ``QTextDocument``. The getter is just an
        # integer read so it's safe to call on every keystroke.
        self._update_line_number_area_width(self.editor.document().blockCount())

    def _on_editor_scrolled(self, _value: int) -> None:
        """Repaint the gutter when the user scrolls vertically.

        ``QTextEdit`` doesn't expose a partial-update signal with a
        delta, but the gutter's paintEvent always walks from
        ``firstVisibleBlock()`` to compute each line's y-coordinate —
        so a full repaint on every scroll tick is correct and cheap.
        Triggering a single ``update()`` keeps it at one paint per
        scroll event instead of many.
        """
        if not self._line_numbers_enabled:
            return
        self._line_number_area.update()

    def resizeEvent(self, event) -> None:  # noqa: D401
        """Repaint the gutter on viewport resize.

        The gutter shares an ``QHBoxLayout`` with the editor; when
        the editor's vertical scrollbar appears/disappears (e.g. user
        shrinks the window vertically until lines wrap), the gutter
        width and visible-line range both change.
        """
        super().resizeEvent(event)
        if self._line_numbers_enabled:
            self._line_number_area.update()

    def _on_editor_font_changed(self, _font: QFont) -> None:
        """Repaint the gutter when the editor font changes.

        Font size affects digit width and line height, both of which
        feed ``sizeHint`` / paint positions.
        """
        if not self._line_numbers_enabled:
            return
        self._line_number_area.updateGeometry()
        self._line_number_area.update()

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

    # ---- font sizing -------------------------------------------------------

    MIN_FONT_POINT_SIZE = 8
    MAX_FONT_POINT_SIZE = 32
    # Default bumped from 12 → 14 in the multi-bug-fix milestone (issue
    # #2). The user's original complaint: "Default font size for UI and
    # for content should be 14". ``reset_font_size`` uses this constant
    # for Ctrl+0, so first-time users who never adjust their font end
    # up at 14pt after a reset; existing users who deliberately picked
    # a different size are unaffected until they hit Ctrl+0.
    DEFAULT_FONT_POINT_SIZE = 14

    def adjust_font_size(self, delta: int) -> None:
        """Increase or decrease the editor font size by delta points."""
        font = self.editor.font()
        new_size = max(
            self.MIN_FONT_POINT_SIZE,
            min(self.MAX_FONT_POINT_SIZE, font.pointSize() + delta),
        )
        font.setPointSize(new_size)
        self.editor.setFont(font)
        # QTextEdit has no fontChanged signal — drive the gutter update
        # from the only call site that mutates the editor font.
        self._on_editor_font_changed(font)

    def reset_font_size(self) -> None:
        """Reset editor font to default size."""
        font = self.editor.font()
        font.setPointSize(self.DEFAULT_FONT_POINT_SIZE)
        self.editor.setFont(font)
        self._on_editor_font_changed(font)