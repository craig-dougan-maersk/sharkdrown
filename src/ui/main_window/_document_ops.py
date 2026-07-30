"""Document operations mixin — tab management, file I/O, split/preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QTabBar,
    QWidget,
)

from src.core.document import DocumentTab


# ---------------------------------------------------------------------------
# Format → colour and short-tag mapping for editor tabs
# ---------------------------------------------------------------------------

_FORMAT_COLORS: dict[str, str] = {
    "markdown": "#4a9eff",
    "json": "#e8a838",
    "yaml": "#4ec86c",
    "text": "#888888",
    "html": "#c678dd",
}

# Short tag text shown in the tab label before the filename. The tag
# is a format identifier (not the actual file extension) — so a .yml
# file shows the tag "yaml" while a .yaml file also shows "yaml".
_FORMAT_TAGS: dict[str, str] = {
    "markdown": "md",
    "json": "json",
    "yaml": "yaml",
    "text": "txt",
    "html": "html",
}

# Default file extension assigned to a brand-new untitled tab of the
# given format. Note ``yaml`` -> ``yml`` per the spec.
_DEFAULT_EXTENSIONS: dict[str, str] = {
    "markdown": "md",
    "json": "json",
    "yaml": "yml",
    "text": "txt",
    "html": "html",
}


# ---------------------------------------------------------------------------
# Starter content for new files
# ---------------------------------------------------------------------------

_STARTER_CONTENT: dict[str, str] = {
    "json": "{\n  \n}\n",
    "yaml": "---\n",
    "markdown": "",
    "text": "",
    "html": (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <title></title>\n"
        "</head>\n<body>\n</body>\n</html>\n"
    ),
}


# ---------------------------------------------------------------------------
# Format detection from a bare extension string
# ---------------------------------------------------------------------------

def _detect_format_from_ext(ext: str) -> str:
    """Map a (lowercased, dotted) extension to a format.

    Unknown or empty extensions fall back to ``"text"`` so a renamed
    file with an unrecognised suffix becomes a plain text document.
    """
    return {
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".txt": "text",
        ".html": "html",
        ".htm": "html",
    }.get(ext.lower(), "text")


# ---------------------------------------------------------------------------
# Custom tab label widget
# ---------------------------------------------------------------------------

class _TabLabelWidget(QWidget):
    """Custom tab label: a coloured format tag + the filename.

    Used in place of HTML in ``setTabText`` because ``QTabBar``'s
    built-in label rendering only supports a tiny subset of HTML and
    on many platforms/styles it shows the markup literally. A real
    widget placed via ``QTabBar.setTabButton(LeftSide, ...)`` is
    always rendered correctly.

    The tag is a coloured "chip" (rounded background, white bold
    text) that matches the colour previously used for the round
    dot icon. The filename sits to the right of the chip and is
    italic when the document is dirty.
    """

    def __init__(
        self,
        tag: str,
        color: str,
        name: str,
        italic: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Exposed for tests / debugging — useful attributes to have on
        # hand rather than walking the child QLabels.
        self.tag = tag
        self.color = color
        self.name = name
        self.italic = italic

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        # ---- Format tag chip ---------------------------------------
        self._tag_label = QLabel(tag)
        tag_font = self._tag_label.font()
        tag_font.setBold(True)
        self._tag_label.setFont(tag_font)
        # ``setStyleSheet`` on a QLabel scopes the colour/background
        # to the chip, not the whole tab.
        self._tag_label.setStyleSheet(
            f"background-color: {color}; color: white;"
            f" padding: 1px 6px; border-radius: 3px;"
        )
        # Keep the chip compact — it shouldn't stretch to fill the tab.
        self._tag_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        layout.addWidget(self._tag_label)

        # ---- Filename ----------------------------------------------
        self._name_label = QLabel(name)
        if italic:
            name_font = self._name_label.font()
            name_font.setItalic(True)
            self._name_label.setFont(name_font)
        layout.addWidget(self._name_label)

        # Push the chip+name to the left so the close button (placed
        # by the tab bar on the right) has room to sit at the end.
        layout.addStretch()


class DocumentOpsMixin:
    """Editor tab management, file operations, and split/preview controls."""

    # Track the previously active tab index for cursor saving
    _prev_editor_index: int = -1
    _lint_timer: QTimer | None = None

    # ---- New file with dialog -----------------------------------------------

    def _new_file_with_dialog(self) -> None:
        """Show the New File dialog and create a tab for the chosen type."""
        from src.ui.dialogs.new_file_dialog import NewFileDialog

        fmt = NewFileDialog.ask(self)
        if fmt is None:
            return
        self._new_untitled_tab(fmt)

    def _new_untitled_tab(self, format_type: str) -> None:
        """Create a brand-new untitled tab for the given format.

        The tab is named ``untitled.{ext}`` where ``ext`` is the
        format's default extension. If a tab with that name is already
        open, a ``(N)`` suffix is appended and incremented until unique
        (so the second JSON file is ``untitled (2).json``, the third
        is ``untitled (3).json``, and so on).
        """
        ext = _DEFAULT_EXTENSIONS.get(format_type, "txt")
        base_name = f"untitled.{ext}"
        name = self._dedup_tab_name(base_name)
        content = _STARTER_CONTENT.get(format_type, "")
        self._create_new_tab(
            content=content,
            format_type=format_type,
            display_name=name,
        )

    def _dedup_tab_name(self, base_name: str) -> str:
        """Return ``base_name`` or a ``(N)``-suffixed variant if already in use.

        Scans the currently-open editor tabs and only counts a name as
        taken if it appears verbatim. If only ``untitled (2).json`` is
        open, a new JSON file is named ``untitled.json`` (not
        ``untitled (3).json``) — the gap is left alone.
        """
        existing: set[str] = set()
        for i in range(self.editor_tabs.count()):
            editor = self.editor_tabs.widget(i)
            if editor is not None and editor.document is not None:
                existing.add(editor.document.title())

        if base_name not in existing:
            return base_name

        if "." in base_name:
            dot = base_name.rindex(".")
            base, ext = base_name[:dot], base_name[dot:]
        else:
            base, ext = base_name, ""

        n = 2
        while f"{base} ({n}){ext}" in existing:
            n += 1
        return f"{base} ({n}){ext}"

    # ---- Tab creation -------------------------------------------------------

    def _create_new_tab(
        self,
        content: str = "",
        file_path: Path | None = None,
        format_type: str | None = None,
        display_name: str | None = None,
    ) -> None:
        """Create a new editor tab with optional content.

        ``display_name`` is an override shown in the tab title — used
        for brand-new untitled tabs (``untitled.json``,
        ``untitled (2).json``, …) so they show a meaningful name
        before they're ever saved to disk.
        """
        doc_tab = DocumentTab(file_path, content, format_type=format_type)
        if display_name is not None:
            doc_tab.display_name = display_name

        from src.ui.widgets.editor_widget import EditorWidget
        editor = EditorWidget()
        editor.document = doc_tab

        index = self.editor_tabs.addTab(editor, "")
        # Route the title through ``_update_tab_title`` so the format
        # tag and italic/roman state are set the same way as every
        # later refresh.
        self._update_tab_title(index, doc_tab)
        self.editor_tabs.setCurrentIndex(index)

        editor.text_changed.connect(lambda idx=index: self._update_tab_title(idx, doc_tab))
        editor.content_changed.connect(
            lambda text, idx=index: self._on_editor_content_changed(idx, doc_tab)
        )

        if self.split_enabled and self.preview_visible:
            self._create_preview_for(index, doc_tab)

    def _create_preview_for(self, editor_index: int, doc_tab: DocumentTab) -> None:
        """Create a preview tab paired to the given editor tab."""
        from src.ui.widgets.preview_widget import PreviewWidget

        preview = PreviewWidget()
        preview.set_document(doc_tab)

        pindex = self.preview_tabs.addTab(preview, f"Preview: {doc_tab.title()}")
        self.preview_tabs.setCurrentIndex(pindex)

        self._tab_pairs[editor_index] = pindex
        self._preview_docs[pindex] = doc_tab

    # ---- Tab title / icon ---------------------------------------------------

    def _update_tab_title(self, index: int, doc_tab: DocumentTab) -> None:
        """Render the tab label as ``[format tag] [filename]``.

        The tag is the short format identifier (``md``, ``json``,
        ``yaml``, ``txt``, ``html``) shown as a coloured chip. The
        filename is italic when the document is dirty and roman when
        it matches the file on disk.

        Uses a real ``_TabLabelWidget`` placed via
        ``QTabBar.setTabButton(LeftSide, ...)`` rather than HTML in
        ``setTabText`` — the tab bar's built-in label rendering only
        supports a tiny subset of HTML and on many platforms it shows
        the markup literally.
        """
        tag = _FORMAT_TAGS.get(doc_tab.format_type, doc_tab.format_type)
        color = _FORMAT_COLORS.get(doc_tab.format_type, "#888888")
        name = doc_tab.title()
        italic = doc_tab.is_modified()

        tab_bar = self.editor_tabs.tabBar()
        # The previous widget (if any) is owned by the tab bar until
        # we replace it; calling ``deleteLater()`` after detaching it
        # frees the old widget's memory.
        old_widget = tab_bar.tabButton(index, QTabBar.LeftSide)
        if old_widget is not None:
            old_widget.setParent(None)
            old_widget.deleteLater()

        label_widget = _TabLabelWidget(tag, color, name, italic)
        tab_bar.setTabButton(index, QTabBar.LeftSide, label_widget)
        # Clear the default style-drawn text so the custom widget is
        # the only thing visible in the tab's left slot.
        self.editor_tabs.setTabText(index, "")
        self.editor_tabs.setTabIcon(index, QIcon())

        if index in self._tab_pairs:
            pindex = self._tab_pairs[index]
            self.preview_tabs.setTabText(pindex, f"Preview: {doc_tab.title()}")

    # ---- Tab rename via double-click ----------------------------------------

    def _install_tab_rename_filter(self) -> None:
        """Install event filter on editor_tabs tab bar for double-click rename."""
        self.editor_tabs.tabBar().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        """Detect double-click on tab bar to trigger rename."""
        if obj is self.editor_tabs.tabBar() and event.type() == QEvent.MouseButtonDblClick:
            index = self.editor_tabs.tabBar().tabAt(event.pos())
            if index >= 0:
                self._rename_tab(index)
                return True
        return super().eventFilter(obj, event)

    def _rename_tab(self, index: int) -> None:
        """Rename the tab's file via double-click.

        If the extension changes, the user is warned and the format is
        updated to match the new extension (or falls back to plain
        text for unrecognised / missing extensions). For tabs backed
        by a real file on disk, the rename updates ``file_path`` to
        the new name in the same directory; for untitled tabs it
        updates ``display_name`` instead. The actual file on disk is
        only created/renamed when the user saves — until then the tab
        renders italic (because the in-memory content no longer
        matches the new file path, which doesn't exist yet).
        """
        editor = self.editor_tabs.widget(index)
        if editor is None or editor.document is None:
            return
        doc_tab = editor.document
        current_name = doc_tab.title()

        new_name, ok = QInputDialog.getText(
            self, "Rename Tab", "New name:", text=current_name
        )
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()

        # ``Path(name).suffix`` returns ``""`` for names with no dot in
        # the leaf — so a file literally called ``untitled`` is
        # correctly treated as having no extension.
        current_ext = Path(current_name).suffix.lower()
        new_ext = Path(new_name).suffix.lower()

        if new_ext != current_ext:
            reply = QMessageBox.question(
                self,
                "Change File Type?",
                f"You changed the extension from "
                f"'{current_ext or '(none)'}' to '{new_ext or '(none)'}'.\n\n"
                f"The file type will be updated to match the new "
                f"extension. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            doc_tab.format_type = _detect_format_from_ext(new_ext)
            if hasattr(editor, "_update_highlighter"):
                editor._update_highlighter()
            # The format tag in the tab depends on format_type, so
            # re-render the title after the format change.
            self._update_markdown_toolbar_visibility()

        # Apply the new name. If the tab has a backing file, move the
        # file path; otherwise the title is just a display override.
        if doc_tab.file_path is not None:
            doc_tab.file_path = doc_tab.file_path.parent / new_name
            doc_tab.display_name = None
        else:
            doc_tab.display_name = new_name

        self._update_tab_title(index, doc_tab)

    # ---- Content change / lint trigger --------------------------------------

    def _on_editor_content_changed(
        self, editor_index: int, doc_tab: DocumentTab
    ) -> None:
        """Re-render preview and schedule lint."""
        if editor_index in self._tab_pairs:
            pindex = self._tab_pairs[editor_index]
            preview = self.preview_tabs.widget(pindex)
            if preview is not None:
                preview._render_preview()

        self._schedule_lint()
        self._update_document_stats()

    def _schedule_lint(self) -> None:
        """Debounced lint — waits 500ms after last edit."""
        if self._lint_timer is None:
            self._lint_timer = QTimer(self)
            self._lint_timer.setSingleShot(True)
            self._lint_timer.timeout.connect(self._run_lint)
        self._lint_timer.start(500)

    def _run_lint(self) -> None:
        """Run the appropriate linter on the active document."""
        if not hasattr(self, "lint_panel"):
            return
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None or editor.document is None:
            return

        from src.ui.widgets.lint_panel import lint_text
        issues = lint_text(editor.toPlainText(), editor.document.format_type)
        self.lint_panel.set_issues(issues)

    def _on_lint_issue_clicked(self, line: int, col: int) -> None:
        """Move editor cursor to the lint issue location."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None:
            return
        cursor = editor.editor.textCursor()
        block = editor.editor.document().findBlockByNumber(line - 1)
        if block.isValid():
            pos = block.position() + min(col - 1, block.length() - 1)
            cursor.setPosition(max(0, pos))
            editor.editor.setTextCursor(cursor)
            editor.editor.setFocus()

    # ---- Status bar stats ---------------------------------------------------

    def _update_document_stats(self) -> None:
        """Update word count, line count labels in status bar."""
        if not hasattr(self, "word_count_label"):
            return
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None:
            return
        text = editor.toPlainText()
        words = len(text.split()) if text.strip() else 0
        lines = text.count("\n") + 1 if text else 0
        self.word_count_label.setText(f"Words: {words}")
        self.line_count_label.setText(f"Lines: {lines}")

    def _update_cursor_position(self) -> None:
        """Update cursor position label from active editor."""
        if not hasattr(self, "cursor_pos_label"):
            return
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None:
            return
        cursor = editor.editor.textCursor()
        ln = cursor.blockNumber() + 1
        col = cursor.columnNumber() + 1
        self.cursor_pos_label.setText(f"Ln {ln}, Col {col}")

    # ---- Cursor memory (per-tab) -------------------------------------------

    def _save_cursor_for_tab(self, index: int) -> None:
        """Save cursor position for the tab at index."""
        if index < 0:
            return
        editor = self.editor_tabs.widget(index)
        if editor is None or editor.document is None:
            return
        cursor = editor.editor.textCursor()
        editor.document.cursor_block = cursor.blockNumber()
        editor.document.cursor_column = cursor.columnNumber()

    def _restore_cursor_for_tab(self, index: int) -> None:
        """Restore saved cursor position for the tab at index."""
        if index < 0:
            return
        editor = self.editor_tabs.widget(index)
        if editor is None or editor.document is None:
            return
        doc_tab = editor.document
        cursor = editor.editor.textCursor()
        block = editor.editor.document().findBlockByNumber(doc_tab.cursor_block)
        if block.isValid():
            pos = block.position() + min(doc_tab.cursor_column, max(0, block.length() - 1))
            cursor.setPosition(pos)
            editor.editor.setTextCursor(cursor)

    # ---- File I/O -----------------------------------------------------------

    def _open_file(self) -> None:
        """Open a file dialog and load the selected file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open File",
            str(Path.home()),
            "All Supported Files (*.md *.markdown *.json *.yaml *.yml *.txt);;"
            "Markdown Files (*.md *.markdown);;"
            "JSON Files (*.json);;"
            "YAML Files (*.yaml *.yml);;"
            "Text Files (*.txt);;"
            "All Files (*)",
        )
        if file_path:
            self._load_file(Path(file_path))

    def _load_file(self, file_path: Path) -> None:
        """Load a file into a new tab."""
        try:
            content = file_path.read_text(encoding="utf-8")
            self._create_new_tab(content, file_path)
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.critical(self, "Error", f"Failed to open file: {e}")

    def _save_file(self) -> None:
        """Save the current editor tab to file."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return

        editor = self.editor_tabs.widget(current_idx)
        doc_tab = editor.document

        if doc_tab.file_path is None:
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save File",
                str(Path.home() / "untitled.md"),
                "Markdown Files (*.md *.markdown);;"
                "JSON Files (*.json);;"
                "YAML Files (*.yaml *.yml);;"
                "Text Files (*.txt);;"
                "All Files (*)",
            )
            if not file_path:
                return
            doc_tab.file_path = Path(file_path)
            doc_tab.format_type = doc_tab._detect_format()
            editor._update_highlighter()
        else:
            file_path = doc_tab.file_path

        try:
            file_path.write_text(editor.toPlainText(), encoding="utf-8")
            doc_tab.content = editor.toPlainText()
            self._update_tab_title(current_idx, doc_tab)
            self.status.showMessage(f"Saved: {file_path.name}", 3000)
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Failed to save file: {e}")

    def _save_all(self) -> None:
        """Save all editor tabs."""
        for i in range(self.editor_tabs.count()):
            editor = self.editor_tabs.widget(i)
            doc_tab = editor.document
            if doc_tab.file_path:
                try:
                    doc_tab.file_path.write_text(editor.toPlainText(), encoding="utf-8")
                    doc_tab.content = editor.toPlainText()
                except OSError as e:
                    QMessageBox.warning(self, "Warning", f"Failed to save {doc_tab.title()}: {e}")
        self.status.showMessage("All files saved", 3000)

    # ---- Tab close (with unsaved guard) ------------------------------------

    def _close_tab(self, index: int) -> None:
        """Close an editor tab, prompting to save unsaved changes."""
        editor = self.editor_tabs.widget(index)
        if editor is not None and editor.document is not None:
            doc_tab = editor.document
            if doc_tab.is_modified():
                reply = QMessageBox.question(
                    self,
                    "Unsaved Changes",
                    f"'{doc_tab.title()}' has unsaved changes.\n\nSave before closing?",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                    QMessageBox.Save,
                )
                if reply == QMessageBox.Cancel:
                    return
                if reply == QMessageBox.Save:
                    old_idx = self.editor_tabs.currentIndex()
                    self.editor_tabs.setCurrentIndex(index)
                    self._save_file()
                    self.editor_tabs.setCurrentIndex(old_idx)

        if index in self._tab_pairs:
            pindex = self._tab_pairs[index]
            self.preview_tabs.removeTab(pindex)
            del self._tab_pairs[index]
        self.editor_tabs.removeTab(index)

    def _close_preview_tab(self, index: int) -> None:
        """Close a preview tab."""
        for e_idx, p_idx in list(self._tab_pairs.items()):
            if p_idx == index:
                self.editor_tabs.removeTab(e_idx)
                del self._tab_pairs[e_idx]
                break

    def _close_current_tab(self) -> None:
        """Close the current tab."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx >= 0:
            self._close_tab(current_idx)

    # ---- Tab change ---------------------------------------------------------

    def _on_editor_tab_changed(self, index: int) -> None:
        """Handle editor tab change — cursor memory, toolbar, format sync."""
        # Save cursor for the tab we're leaving
        self._save_cursor_for_tab(self._prev_editor_index)
        self._prev_editor_index = index

        # Restore cursor for the new tab
        self._restore_cursor_for_tab(index)

        # Sync preview
        if index in self._tab_pairs:
            self.preview_tabs.setCurrentIndex(self._tab_pairs[index])

        # Sync format combo
        self._sync_format_combo_to_current_document()

        # Show/hide markdown toolbar
        self._update_markdown_toolbar_visibility()

        # Connect cursor position tracking
        self._connect_cursor_tracking(index)

        # Update stats
        self._update_document_stats()
        self._update_cursor_position()

        # Trigger lint for newly active tab
        self._schedule_lint()

    def _update_markdown_toolbar_visibility(self) -> None:
        """Show markdown toolbar only when active tab is markdown."""
        if not hasattr(self, "markdown_toolbar"):
            return
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            self.markdown_toolbar.setVisible(False)
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None or editor.document is None:
            self.markdown_toolbar.setVisible(False)
            return
        is_md = editor.document.format_type == "markdown"
        self.markdown_toolbar.setVisible(is_md)

        # Point toolbar at the active editor's QTextEdit
        self.markdown_toolbar.set_editor(editor.editor if is_md else None)

    def _connect_cursor_tracking(self, index: int) -> None:
        """Wire cursor position updates to the newly focused editor."""
        if not hasattr(self, "_cursor_connection"):
            self._cursor_connection = None

        # Disconnect previous
        if self._cursor_connection is not None:
            try:
                self._cursor_connection.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._cursor_connection = None

        if index < 0:
            return
        editor = self.editor_tabs.widget(index)
        if editor is None:
            return
        self._cursor_connection = editor.editor.cursorPositionChanged
        self._cursor_connection.connect(self._update_cursor_position)

    def _on_preview_tab_changed(self, index: int) -> None:
        """Handle preview tab change."""
        pass

    # ---- Split / Preview controls ------------------------------------------

    def _apply_view_state(self) -> None:
        """Apply the current split/preview visibility state to the UI."""
        self.preview_tabs.setVisible(self.split_enabled)

        if self.split_enabled:
            total = max(1, self.splitter.width())
            half = total // 2
            self.splitter.setSizes([half, total - half])

        if self.split_enabled:
            current = self.editor_tabs.currentIndex()
            if current in self._tab_pairs:
                self.preview_tabs.setCurrentIndex(self._tab_pairs[current])

    def _toggle_split(self) -> None:
        """Toggle the split view on/off."""
        self.split_enabled = not self.split_enabled
        self.action_toggle_split.setChecked(self.split_enabled)
        self._apply_view_state()

    def _toggle_preview(self) -> None:
        """Toggle the preview visibility."""
        self.preview_visible = not self.preview_visible

        if self.preview_visible:
            self.split_enabled = True
            self.action_toggle_split.setChecked(True)

            current = self.editor_tabs.currentIndex()
            if current >= 0 and current not in self._tab_pairs:
                editor = self.editor_tabs.widget(current)
                self._create_preview_for(current, editor.document)

        self.action_toggle_preview.setChecked(self.preview_visible)
        self._apply_view_state()

    def _set_split_orientation(self, orientation: Qt.Orientation) -> None:
        """Set the split orientation (horizontal or vertical)."""
        self.splitter.setOrientation(orientation)

    def _set_current_format(self, format_type: str) -> None:
        """Set the current document's format and re-render the preview."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None or editor.document is None:
            return
        editor.document.format_type = format_type
        # The coloured-dot tab icon is gone — refresh the rich-text
        # tag instead so the new format's colour shows immediately.
        self._update_tab_title(current_idx, editor.document)
        self._on_editor_content_changed(current_idx, editor.document)
        if hasattr(editor, "_update_highlighter"):
            editor._update_highlighter()
        self._update_markdown_toolbar_visibility()
