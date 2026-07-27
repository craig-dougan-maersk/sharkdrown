"""Document operations mixin — tab management, file I/O, split/preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox

from src.core.document import DocumentTab


class DocumentOpsMixin:
    """Editor tab management, file operations, and split/preview controls."""

    def _create_new_tab(self, content: str = "", file_path: Path | None = None) -> None:
        """Create a new editor tab with optional content."""
        doc_tab = DocumentTab(file_path, content)

        from src.ui.widgets.editor_widget import EditorWidget
        editor = EditorWidget()
        editor.document = doc_tab

        index = self.editor_tabs.addTab(editor, doc_tab.title())
        self.editor_tabs.setCurrentIndex(index)

        editor.text_changed.connect(lambda: self._update_tab_title(index, doc_tab))
        editor.content_changed.connect(
            lambda text: self._on_editor_content_changed(index, doc_tab)
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

    def _update_tab_title(self, index: int, doc_tab: DocumentTab) -> None:
        """Update tab title when file is saved or renamed."""
        self.editor_tabs.setTabText(index, doc_tab.title())
        if index in self._tab_pairs:
            pindex = self._tab_pairs[index]
            self.preview_tabs.setTabText(pindex, f"Preview: {doc_tab.title()}")

    def _on_editor_content_changed(
        self, editor_index: int, doc_tab: DocumentTab
    ) -> None:
        """Re-render the preview paired with editor_index, if any."""
        if editor_index not in self._tab_pairs:
            return
        pindex = self._tab_pairs[editor_index]
        preview = self.preview_tabs.widget(pindex)
        if preview is not None:
            preview._render_preview()

    def _open_file(self) -> None:
        """Open a file dialog and load the selected file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open File",
            str(Path.home()),
            "All Supported Files (*.md *.markdown *.json *.yaml *.yml);;Markdown Files (*.md *.markdown);;JSON Files (*.json);;YAML Files (*.yaml *.yml)",
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
                "Markdown Files (*.md *.markdown);;JSON Files (*.json);;YAML Files (*.yaml *.yml);;All Files (*)",
            )
            if file_path:
                doc_tab.file_path = Path(file_path)
        else:
            file_path = doc_tab.file_path

        try:
            file_path.write_text(editor.toPlainText(), encoding="utf-8")
            doc_tab.content = editor.toPlainText()
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

    def _close_tab(self, index: int) -> None:
        """Close an editor tab."""
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

    def _on_editor_tab_changed(self, index: int) -> None:
        """Handle editor tab change."""
        if index in self._tab_pairs:
            self.preview_tabs.setCurrentIndex(self._tab_pairs[index])
        self._sync_format_combo_to_current_document()

    def _on_preview_tab_changed(self, index: int) -> None:
        """Handle preview tab change."""
        pass

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
        self._on_editor_content_changed(current_idx, editor.document)
        if hasattr(editor, "_update_highlighter"):
            editor._update_highlighter()
