"""Markdown formatting toolbar for the Documents editor.

Provides quick markdown formatting actions for the active ``QTextEdit``.
The toolbar is shown only when the active document is markdown
(``_update_markdown_toolbar_visibility`` in ``_document_ops.py``); for
JSON/YAML it hides itself so its dead buttons don't mislead.

Layout (left → right):
    **Bold**, *Italic*, ~~Strikethrough~~, `Inline Code` | **H**▾
    (Heading 1–5) | • Bullet List, 1. Numbered List, ☐ Task List |
    " Blockquote | 🔗 Link, 🖼 Image, ▦ Table | ``` Code Block,
    🛈 Mermaid Diagram, ─ Horizontal Rule | ↶ Undo, ↷ Redo

The heading group uses a single ``QToolButton`` with
``InstantPopup`` mode — clicking it opens a nested menu of H1–H5,
rather than triggering the action on the parent button itself.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import (
    QAction,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QTextCursor,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMenu,
    QTextEdit,
    QToolBar,
    QToolButton,
)

_ICONS_DIR = Path(__file__).resolve().parents[3] / "src" / "resources" / "icons" / "markdown"


def _load_icon(name: str) -> QIcon:
    """Render an SVG icon from the markdown icons directory.

    ``QSvgRenderer`` has no ``to_pixmap`` shortcut in PySide6 — we have
    to allocate a ``QPixmap`` ourselves and call ``render`` with a
    ``QPainter``. The SVG uses ``fill="currentColor"`` so Qt 6.5+
    recolours the rendered pixmap against the destination widget's
    palette, giving us a single icon that works for light/dark themes.
    """
    path = _ICONS_DIR / name
    renderer = QSvgRenderer(str(path))
    pixmap = QPixmap(QSize(24, 24))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()

    icon = QIcon(pixmap)
    # Also add the raw file path as a fallback — QIcon can rasterise
    # SVGs directly when the pixmap doesn't match the device pixel
    # ratio, and this keeps Hi-DPI screens crisp.
    icon.addFile(str(path))
    return icon


class MarkdownToolbar(QToolBar):
    """Toolbar providing quick markdown formatting actions.

    Operates on whichever ``QTextEdit`` is set via ``set_editor()``.
    Undo/redo are bound to the editor's own undo stack and reflect
    that stack's availability via ``redoAvailable`` /
    ``undoAvailable`` signals.
    """

    def __init__(self, parent=None) -> None:
        super().__init__("Markdown", parent)
        self._editor: QTextEdit | None = None
        self._build_actions()
        self.setIconSize(QSize(20, 20))

    def set_editor(self, editor: QTextEdit | None) -> None:
        """Point the toolbar at a different editor.

        Disconnects the previous editor's undo/redo signals and wires
        the new one so Undo/Redo enable/disable based on stack state.
        Passing ``None`` disables both.

        ``QTextEdit`` has no ``isUndoAvailable()``/``isRedoAvailable()``
        accessors (those live on ``QPlainTextEdit``), so we seed the
        initial state by emitting a synthetic ``undoAvailable(True)``
        *only when there is content* — a brand-new empty document has
        nothing to undo, but the user's first edit fires the real
        signal and corrects it before the user can notice.
        """
        prev = self._editor
        if prev is not None:
            try:
                prev.undoAvailable.disconnect(self._on_undo_available)
            except (RuntimeError, TypeError):
                pass
            try:
                prev.redoAvailable.disconnect(self._on_redo_available)
            except (RuntimeError, TypeError):
                pass

        self._editor = editor

        if editor is None:
            self._action_undo.setEnabled(False)
            self._action_redo.setEnabled(False)
            return

        editor.undoAvailable.connect(self._on_undo_available)
        editor.redoAvailable.connect(self._on_redo_available)
        # Optimistic seed: assume an undoable history at attach time.
        # The real signal will refine the state on the first user edit.
        # Setting both to True is safer than False — an inactive button
        # that should be enabled is a worse UX surprise than the
        # opposite, and the toolbar only renders a clickable button
        # that calls into a real QTextEdit undo anyway.
        self._on_undo_available(True)
        self._on_redo_available(False)

    # ---- action wiring ---------------------------------------------------

    def _build_actions(self) -> None:
        # Inline emphasis
        self._add_icon_button(
            "bold.svg", "Bold (Ctrl+B)", lambda: self._wrap("**", "**")
        )
        self._add_icon_button(
            "italic.svg", "Italic (Ctrl+I)", lambda: self._wrap("*", "*")
        )
        self._add_icon_button(
            "strikethrough.svg",
            "Strikethrough",
            lambda: self._wrap("~~", "~~"),
        )
        self._add_icon_button(
            "inline-code.svg", "Inline code", lambda: self._wrap("`", "`")
        )

        self.addSeparator()

        # Heading group — single button with a nested popup menu so the
        # toolbar row stays compact while still offering all five levels.
        heading_btn = QToolButton(self)
        heading_btn.setIcon(_load_icon("heading.svg"))
        heading_btn.setToolTip("Heading level")
        heading_btn.setText("H")
        heading_btn.setPopupMode(QToolButton.InstantPopup)
        heading_menu = QMenu(heading_btn)
        for level in range(1, 6):
            action = QAction(f"Heading {level}", heading_btn)
            action.triggered.connect(
                lambda _checked=False, lvl=level: self._prefix_line("#" * lvl + " ")
            )
            heading_menu.addAction(action)
        heading_btn.setMenu(heading_menu)
        self.addWidget(heading_btn)

        self.addSeparator()

        # Lists
        self._add_icon_button(
            "bullet-list.svg", "Bullet list", lambda: self._prefix_line("- ")
        )
        self._add_icon_button(
            "numbered-list.svg", "Numbered list", lambda: self._prefix_line("1. ")
        )
        self._add_icon_button(
            "task-list.svg", "Task list", lambda: self._prefix_line("- [ ] ")
        )

        self.addSeparator()

        # Block
        self._add_icon_button(
            "blockquote.svg", "Blockquote", lambda: self._prefix_line("> ")
        )

        self.addSeparator()

        # Insertions
        self._add_icon_button("link.svg", "Insert link", self._insert_link)
        self._add_icon_button(
            "image.svg", "Insert image", self._insert_image
        )
        self._add_icon_button("table.svg", "Insert table", self._insert_table)
        self._add_icon_button(
            "code-block.svg", "Code block", self._insert_code_block
        )
        self._add_icon_button(
            "mermaid.svg", "Mermaid diagram", self._insert_mermaid
        )
        self._add_icon_button(
            "horizontal-rule.svg",
            "Horizontal rule",
            self._insert_horizontal_rule,
        )

        self.addSeparator()

        # Undo/redo — wired to QTextEdit's own undo stack
        self._action_undo = QAction("Undo", self)
        self._action_undo.setIcon(_load_icon("undo.svg"))
        self._action_undo.setToolTip("Undo (Ctrl+Z)")
        self._action_undo.setShortcut(QKeySequence.Undo)
        self._action_undo.triggered.connect(self._undo)
        self.addAction(self._action_undo)

        self._action_redo = QAction("Redo", self)
        self._action_redo.setIcon(_load_icon("redo.svg"))
        self._action_redo.setToolTip("Redo (Ctrl+Shift+Z)")
        self._action_redo.setShortcut(QKeySequence.Redo)
        self._action_redo.triggered.connect(self._redo)
        self.addAction(self._action_redo)

        # Both start disabled until an editor is attached and signals
        # say the stack has entries.
        self._action_undo.setEnabled(False)
        self._action_redo.setEnabled(False)

    def _add_icon_button(
        self, icon_name: str, tooltip: str, triggered
    ) -> None:
        """Add an icon-only QToolButton action to the toolbar.

        We use ``QToolButton`` rather than ``addAction`` because
        ``QToolButton`` renders the icon centred with a tooltip popup
        and inherits the toolbar's hover/pressed state styling
        uniformly — the bare ``addAction(QIcon)`` path leaves the
        text-less action looking flat on some themes.
        """
        btn = QToolButton(self)
        btn.setIcon(_load_icon(icon_name))
        btn.setToolTip(tooltip)
        btn.setText("")
        btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        btn.clicked.connect(lambda _checked=False: triggered())
        # Wrap the button in a real toolbar widget so it integrates
        # with the QToolBar layout (separator handling, overflow menu).
        action = self.addWidget(btn)
        action.setToolTip(tooltip)

    # ---- editor callbacks ------------------------------------------------

    def _on_undo_available(self, available: bool) -> None:
        self._action_undo.setEnabled(bool(available))

    def _on_redo_available(self, available: bool) -> None:
        self._action_redo.setEnabled(bool(available))

    def _undo(self) -> None:
        if self._editor is not None:
            self._editor.undo()

    def _redo(self) -> None:
        if self._editor is not None:
            self._editor.redo()

    # ---- primitives ------------------------------------------------------

    def _cursor(self) -> QTextCursor | None:
        if self._editor is None:
            return None
        return self._editor.textCursor()

    def _prefix_line(self, prefix: str) -> None:
        """Insert ``prefix`` at the start of the cursor's block."""
        if self._editor is None:
            return
        cursor = self._editor.textCursor()
        cursor.movePosition(QTextCursor.StartOfBlock)
        cursor.insertText(prefix)
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()

    def _wrap(self, before: str, after: str) -> None:
        """Wrap selection (or insert empty pair) with ``before``/``after``.

        Empty selection: inserts ``before`` and ``after`` with the
        cursor placed between them so the user can type the wrapped
        content immediately. Non-empty selection: wraps the selection
        in place.
        """
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

    def _insert_text(self, text: str, *, place_cursor_at: int | None = None) -> None:
        """Insert ``text`` and place the cursor for natural continuation.

        ``place_cursor_at`` is an *offset from the start of the
        inserted text* where the cursor should land. Used by helpers
        like ``_insert_code_block`` that want the cursor inside the
        newly inserted structure rather than at the end.
        """
        if self._editor is None:
            return
        cursor = self._editor.textCursor()
        start = cursor.position()
        cursor.insertText(text)
        if place_cursor_at is not None:
            cursor.setPosition(start + place_cursor_at)
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()

    # ---- dialogs and complex insertions ----------------------------------

    def _ask_text_url(self, title: str, text_label: str, url_label: str) -> tuple[str, str] | None:
        """Two-field prompt; returns (text, url) or ``None`` if cancelled."""
        if self._editor is None:
            return None

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        text_input = QLineEdit()
        url_input = QLineEdit()
        text_input.setPlaceholderText(text_label)
        url_input.setPlaceholderText(url_label)

        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            text_input.setText(cursor.selectedText())

        form.addRow("Text:", text_input)
        form.addRow(f"{url_label}:", url_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec() != QDialog.Accepted:
            self._editor.setFocus()
            return None
        return text_input.text(), url_input.text()

    def _insert_link(self) -> None:
        result = self._ask_text_url(
            "Insert Link", "Link text", "https://"
        )
        if result is None:
            return
        text, url = result
        text = text or "link"
        url = url or "url"
        self._insert_text(f"[{text}]({url})")

    def _insert_image(self) -> None:
        result = self._ask_text_url(
            "Insert Image", "Alt text", "https://example.com/image.png"
        )
        if result is None:
            return
        text, url = result
        text = text or "alt"
        url = url or "url"
        self._insert_text(f"![{text}]({url})")

    def _insert_table(self) -> None:
        """Insert a 3-column, 3-row starter table.

        3×3 (header row + 2 data rows) is the smallest table most
        editors can usefully extend; wider tables would push the
        cursor off-screen on a fresh document.
        """
        table = (
            "| Column 1 | Column 2 | Column 3 |\n"
            "|----------|----------|----------|\n"
            "|          |          |          |\n"
            "|          |          |          |\n"
        )
        self._insert_text(table)

    def _insert_code_block(self) -> None:
        """Insert a fenced code block; cursor lands inside on its own line."""
        block = "```\n\n```\n"
        # Position the cursor on the empty middle line — ``\n```\n```\n```
        # has the empty line starting at offset 4.
        self._insert_text(block, place_cursor_at=4)

    def _insert_mermaid(self) -> None:
        """Insert a fenced ``mermaid`` block with a sample ``graph TD``."""
        block = "```mermaid\ngraph TD\n    A[Start] --> B[Process]\n    B --> C[End]\n```\n"
        self._insert_text(block)

    def _insert_horizontal_rule(self) -> None:
        """Insert a horizontal rule (``---`` on its own line)."""
        self._insert_text("\n---\n")