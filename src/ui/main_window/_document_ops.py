"""Document operations mixin — tab management, file I/O, split/preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QTextCursor
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from src.core.document import DocumentTab


# ---------------------------------------------------------------------------
# Format → colour mapping for tab icons
# ---------------------------------------------------------------------------

_FORMAT_COLORS: dict[str, str] = {
    "markdown": "#4a9eff",
    "json": "#e8a838",
    "yaml": "#4ec86c",
    "text": "#888888",
}

_icon_cache: dict[str, QIcon] = {}


def _format_icon(format_type: str) -> QIcon:
    """Return a small coloured circle icon for the given format."""
    if format_type in _icon_cache:
        return _icon_cache[format_type]
    color = _FORMAT_COLORS.get(format_type, "#888888")
    px = QPixmap(12, 12)
    px.fill(QColor(0, 0, 0, 0))
    painter = QPainter(px)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(1, 1, 10, 10)
    painter.end()
    icon = QIcon(px)
    _icon_cache[format_type] = icon
    return icon


# ---------------------------------------------------------------------------
# Starter content for new files
# ---------------------------------------------------------------------------

_STARTER_CONTENT: dict[str, str] = {
    "json": "{\n  \n}\n",
    "yaml": "---\n",
    "markdown": "",
    "text": "",
}


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
        content = _STARTER_CONTENT.get(fmt, "")
        self._create_new_tab(content=content, format_type=fmt)

    # ---- Tab creation -------------------------------------------------------

    def _create_new_tab(
        self,
        content: str = "",
        file_path: Path | None = None,
        format_type: str | None = None,
    ) -> None:
        """Create a new editor tab with optional content."""
        doc_tab = DocumentTab(file_path, content, format_type=format_type)

        from src.ui.widgets.editor_widget import EditorWidget
        editor = EditorWidget()
        editor.document = doc_tab

        index = self.editor_tabs.addTab(editor, doc_tab.title())
        self.editor_tabs.setTabIcon(index, _format_icon(doc_tab.format_type))
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
        """Update tab title and icon when content or format changes."""
        title = doc_tab.title()
        if doc_tab.is_modified():
            title = f"● {title}"
        self.editor_tabs.setTabText(index, title)
        self.editor_tabs.setTabIcon(index, _format_icon(doc_tab.format_type))
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
        editor = self.editor_tabs.widget(index)
        if editor is None or editor.document is None:
            return
        doc_tab = editor.document
        current_name = doc_tab.title()
        new_name, ok = QInputDialog.getText(
            self, "Rename Tab", "New name:", text=current_name
        )
        if ok and new_name.strip():
            doc_tab.display_name = new_name.strip()
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
        self.editor_tabs.setTabIcon(current_idx, _format_icon(format_type))
        self._on_editor_content_changed(current_idx, editor.document)
        if hasattr(editor, "_update_highlighter"):
            editor._update_highlighter()
        self._update_markdown_toolbar_visibility()
