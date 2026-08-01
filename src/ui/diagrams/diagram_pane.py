"""Main diagram editor pane — orchestrates palette, canvas, and code preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .codegen.mermaid_generator import MermaidCodeGenerator
from .io.file_io import load_diagram, save_diagram
from .model.connector_types import EndingType, LineStyle
from .model.diagram_document import DiagramDocument
from .model.free_text_model import FreeTextModel
from .palette.shape_palette import ShapePalette
from .preview.code_preview import CodePreview
from .preview.minimap import MiniMap
from .scene.diagram_scene import DiagramScene
from .scene.diagram_view import DiagramView
from .scene.items.connector_item import ConnectorItem
from .scene.items.node_item import DiagramNodeItem
from .toolbar.diagram_toolbar import DiagramToolbar


class DiagramPane(QWidget):
    """A single diagram editor surface.

    Layout:
        [ShapePalette] | [Canvas + MiniMap overlay] | [CodePreview]
    """

    document_modified = Signal()

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self._file_path: Path | None = None
        self._dirty = False

        self._document = DiagramDocument()
        self._generator = MermaidCodeGenerator()

        self._build_ui()
        self._connect_signals()

    def title(self) -> str:
        prefix = "* " if self._dirty else ""
        return f"{prefix}{self._name}"

    @property
    def file_path(self) -> Path | None:
        return self._file_path

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def scene(self) -> DiagramScene:
        return self._scene

    @property
    def document(self) -> DiagramDocument:
        return self._document

    def save(self, path: Path | None = None) -> None:
        if path:
            self._file_path = path
            self._name = path.name
        if not self._file_path:
            return
        save_diagram(self._file_path, self._document)
        self._dirty = False

    def load(self, path: Path) -> None:
        self._file_path = path
        self._name = path.name
        self._document = load_diagram(path)
        self._scene.set_document(self._document)
        self._dirty = False
        self._regenerate_preview()

    def shutdown(self) -> None:
        pass

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)

        self._palette = ShapePalette()
        self._palette.setFixedWidth(180)
        splitter.addWidget(self._palette)

        canvas_container = QWidget()
        canvas_layout = QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)

        self._toolbar = DiagramToolbar()
        canvas_layout.addWidget(self._toolbar)

        self._scene = DiagramScene(self._document)
        self._view = DiagramView(self._scene)
        canvas_layout.addWidget(self._view, 1)

        self._minimap = MiniMap(self._scene, self._view)
        self._minimap.setParent(self._view.viewport())
        self._minimap.setFixedSize(180, 120)
        self._minimap.move(4, self._view.viewport().height() - 124)

        splitter.addWidget(canvas_container)

        self._code_preview = CodePreview()
        self._code_preview.setMinimumWidth(200)
        splitter.addWidget(self._code_preview)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([180, 600, 250])

        layout.addWidget(splitter)

        self._regen_timer = QTimer(self)
        self._regen_timer.setSingleShot(True)
        self._regen_timer.setInterval(300)
        self._regen_timer.timeout.connect(self._regenerate_preview)

    def _connect_signals(self) -> None:
        self._scene.document_changed.connect(self._on_document_changed)
        self._view.zoom_changed.connect(self._toolbar.update_zoom_display)
        self._toolbar.zoom_changed.connect(self._view.set_zoom)
        self._toolbar.zoom_to_fit_requested.connect(self._view.zoom_to_fit)
        self._toolbar.delete_requested.connect(self._scene.remove_selected)
        self._toolbar.fill_color_picked.connect(self._apply_fill_color)
        self._toolbar.outline_color_picked.connect(self._apply_outline_color)
        self._toolbar.add_text_requested.connect(self._add_free_text)
        self._toolbar.connector_mode_toggled.connect(self._scene.set_connector_mode)

        # Connector style drawer
        drawer = self._toolbar._style_drawer
        drawer.line_style_changed.connect(self._apply_connector_style)
        drawer.weight_changed.connect(self._apply_connector_weight)
        drawer.color_changed.connect(self._apply_connector_color)
        drawer.start_ending_changed.connect(self._apply_start_ending)
        drawer.end_ending_changed.connect(self._apply_end_ending)
        drawer.label_changed.connect(self._apply_connector_label)

        self._scene.selectionChanged.connect(self._on_selection_changed)
        self._scene.connector_label_edited.connect(self._on_connector_label_edited)

    def _on_document_changed(self) -> None:
        self._dirty = True
        self._regen_timer.start()
        self.document_modified.emit()

    def _regenerate_preview(self) -> None:
        text = self._generator.generate(self._document)
        self._code_preview.set_mermaid_text(text)

    def _apply_fill_color(self, color: str) -> None:
        for item in self._scene.selectedItems():
            if isinstance(item, DiagramNodeItem):
                item.set_fill_color(color)
        self._scene.document_changed.emit()

    def _apply_outline_color(self, color: str) -> None:
        for item in self._scene.selectedItems():
            if isinstance(item, DiagramNodeItem):
                item.set_outline_color(color)
        self._scene.document_changed.emit()

    def _on_selection_changed(self) -> None:
        selected = self._scene.selectedItems()
        connector_items = [it for it in selected if isinstance(it, ConnectorItem)]
        self._toolbar.set_connector_selection(len(connector_items))
        if connector_items:
            self._toolbar.populate_style_drawer(connector_items[0].model)

    def _on_connector_label_edited(self, connector_id: str, new_text: str) -> None:
        # The model has already been mutated by ConnectorItem.focusOutEvent.
        # document_changed has been emitted via the scene signal.
        del connector_id, new_text

    def _selected_connector_items(self) -> list[ConnectorItem]:
        return [
            it for it in self._scene.selectedItems() if isinstance(it, ConnectorItem)
        ]

    def _apply_connector_style(self, style_value: str) -> None:
        try:
            style = LineStyle(style_value)
        except ValueError:
            return
        for item in self._selected_connector_items():
            item.set_line_style(style)
            self._document.update_connector(item.model)
        if self._selected_connector_items():
            self._scene.document_changed.emit()

    def _apply_connector_weight(self, weight: float) -> None:
        for item in self._selected_connector_items():
            item.set_weight(weight)
            self._document.update_connector(item.model)
        if self._selected_connector_items():
            self._scene.document_changed.emit()

    def _apply_connector_color(self, color: str) -> None:
        for item in self._selected_connector_items():
            item.set_color(color)
            self._document.update_connector(item.model)
            self._toolbar._style_drawer.set_color(color)
        if self._selected_connector_items():
            self._scene.document_changed.emit()

    def _apply_start_ending(self, ending_value: str) -> None:
        try:
            ending = EndingType(ending_value)
        except ValueError:
            return
        for item in self._selected_connector_items():
            item.set_start_ending(ending)
            self._document.update_connector(item.model)
        if self._selected_connector_items():
            self._scene.document_changed.emit()

    def _apply_end_ending(self, ending_value: str) -> None:
        try:
            ending = EndingType(ending_value)
        except ValueError:
            return
        for item in self._selected_connector_items():
            item.set_end_ending(ending)
            self._document.update_connector(item.model)
        if self._selected_connector_items():
            self._scene.document_changed.emit()

    def _apply_connector_label(self, new_label: str) -> None:
        for item in self._selected_connector_items():
            item.set_label(new_label)
            self._document.update_connector(item.model)
        if self._selected_connector_items():
            self._scene.document_changed.emit()

    def _add_free_text(self) -> None:
        center = self._view.mapToScene(
            self._view.viewport().rect().center()
        )
        model = FreeTextModel(text="Text", x=center.x(), y=center.y())
        self._scene.add_free_text(model)

    def keyPressEvent(self, event) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeySequence

        if event.matches(QKeySequence.Copy):
            self._scene.copy_selected()
        elif event.matches(QKeySequence.Paste):
            self._scene.paste_from_clipboard()
        elif event.key() == Qt.Key_Delete or event.key() == Qt.Key_Backspace:
            self._scene.remove_selected()
        elif event.matches(QKeySequence.SelectAll):
            for item in self._scene.items():
                item.setSelected(True)
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        viewport = self._view.viewport()
        self._minimap.move(4, viewport.height() - self._minimap.height() - 4)
