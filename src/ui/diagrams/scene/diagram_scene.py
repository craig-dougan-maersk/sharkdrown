from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QPointF, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QGraphicsScene

from ..model.connector_model import ConnectorModel
from ..model.diagram_document import DiagramDocument
from ..model.free_text_model import FreeTextModel
from ..model.node_model import NodeModel
from .items.connector_item import ConnectorItem
from .items.free_text_item import FreeTextItem
from .items.node_item import DiagramNodeItem

CLIPBOARD_MIME = "application/x-sharkdrown-diagram-items"


class DiagramScene(QGraphicsScene):
    """Graphics scene that syncs with a DiagramDocument model.

    All mutations go through the document first, then the scene updates
    its visual items to match. Emits `document_changed` after any mutation
    so the code preview can regenerate.
    """

    document_changed = Signal()

    def __init__(self, document: DiagramDocument | None = None, parent=None) -> None:
        super().__init__(parent)
        self._document = document or DiagramDocument()
        self._node_items: dict[str, DiagramNodeItem] = {}
        self._connector_items: dict[str, ConnectorItem] = {}
        self._text_items: dict[str, FreeTextItem] = {}

        self.setSceneRect(-2000, -2000, 4000, 4000)

        if self._document.nodes:
            self._sync_from_document()

    @property
    def document(self) -> DiagramDocument:
        return self._document

    def set_document(self, doc: DiagramDocument) -> None:
        self.clear()
        self._node_items.clear()
        self._connector_items.clear()
        self._text_items.clear()
        self._document = doc
        self._sync_from_document()

    def add_node(self, model: NodeModel) -> DiagramNodeItem:
        self._document.add_node(model)
        item = self._create_node_item(model)
        self._update_scene_rect()
        self.document_changed.emit()
        return item

    def remove_node(self, node_id: str) -> None:
        connectors_to_remove = [
            c
            for c in self._document.connectors
            if c.source_id == node_id or c.target_id == node_id
        ]
        for conn in connectors_to_remove:
            self._remove_connector_item(conn.id)

        self._document.remove_node(node_id)
        item = self._node_items.pop(node_id, None)
        if item:
            self.removeItem(item)
        self._update_scene_rect()
        self.document_changed.emit()

    def add_connector(self, model: ConnectorModel) -> ConnectorItem | None:
        src_item = self._node_items.get(model.source_id)
        tgt_item = self._node_items.get(model.target_id)
        if not src_item or not tgt_item:
            return None

        self._document.add_connector(model)
        item = self._create_connector_item(model)
        self.document_changed.emit()
        return item

    def remove_connector(self, connector_id: str) -> None:
        self._document.remove_connector(connector_id)
        self._remove_connector_item(connector_id)
        self.document_changed.emit()

    def add_free_text(self, model: FreeTextModel) -> FreeTextItem:
        self._document.add_free_text(model)
        item = FreeTextItem(model)
        self.addItem(item)
        self._text_items[model.id] = item
        self.document_changed.emit()
        return item

    def remove_free_text(self, text_id: str) -> None:
        self._document.remove_free_text(text_id)
        item = self._text_items.pop(text_id, None)
        if item:
            self.removeItem(item)
        self.document_changed.emit()

    def get_node_item(self, node_id: str) -> DiagramNodeItem | None:
        return self._node_items.get(node_id)

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            if isinstance(item, DiagramNodeItem):
                self.remove_node(item.node_id)
            elif isinstance(item, ConnectorItem):
                self.remove_connector(item.connector_id)
            elif isinstance(item, FreeTextItem):
                self.remove_free_text(item.text_id)

    def copy_selected(self) -> None:
        """Serialize selected nodes and free texts to the clipboard."""
        data: dict = {"nodes": [], "free_texts": []}
        for item in self.selectedItems():
            if isinstance(item, DiagramNodeItem):
                data["nodes"].append(item.model.to_dict())
            elif isinstance(item, FreeTextItem):
                data["free_texts"].append(item.model.to_dict())

        if not data["nodes"] and not data["free_texts"]:
            return

        mime = QMimeData()
        mime.setData(CLIPBOARD_MIME, json.dumps(data).encode("utf-8"))
        QApplication.clipboard().setMimeData(mime)

    def paste_from_clipboard(self) -> None:
        """Deserialize items from clipboard and add with offset."""
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if not mime or not mime.hasFormat(CLIPBOARD_MIME):
            return

        raw = bytes(mime.data(CLIPBOARD_MIME)).decode("utf-8")
        data = json.loads(raw)

        from uuid import uuid4

        offset = 20.0

        self.clearSelection()
        for node_data in data.get("nodes", []):
            node_data["id"] = uuid4().hex[:12]
            node_data["x"] = node_data.get("x", 0) + offset
            node_data["y"] = node_data.get("y", 0) + offset
            model = NodeModel.from_dict(node_data)
            item = self.add_node(model)
            item.setSelected(True)

        for text_data in data.get("free_texts", []):
            text_data["id"] = uuid4().hex[:12]
            text_data["x"] = text_data.get("x", 0) + offset
            text_data["y"] = text_data.get("y", 0) + offset
            model = FreeTextModel.from_dict(text_data)
            item = self.add_free_text(model)
            item.setSelected(True)

    def _sync_from_document(self) -> None:
        for node in self._document.nodes:
            self._create_node_item(node)
        for conn in self._document.connectors:
            self._create_connector_item(conn)
        for text in self._document.free_texts:
            item = FreeTextItem(text)
            self.addItem(item)
            self._text_items[text.id] = item
        self._update_scene_rect()

    def _create_node_item(self, model: NodeModel) -> DiagramNodeItem:
        item = DiagramNodeItem(model)
        item.position_changed.connect(self._on_node_moved)
        self.addItem(item)
        self._node_items[model.id] = item
        return item

    def _create_connector_item(self, model: ConnectorModel) -> ConnectorItem:
        item = ConnectorItem(model)
        self.addItem(item)
        self._connector_items[model.id] = item
        self._update_connector_endpoints(item)
        return item

    def _remove_connector_item(self, connector_id: str) -> None:
        item = self._connector_items.pop(connector_id, None)
        if item:
            self.removeItem(item)

    def _on_node_moved(self, node_id: str, x: float, y: float) -> None:
        for conn in self._document.connectors:
            if conn.source_id == node_id or conn.target_id == node_id:
                item = self._connector_items.get(conn.id)
                if item:
                    self._update_connector_endpoints(item)
        self._update_scene_rect()
        self.document_changed.emit()

    def _update_connector_endpoints(self, item: ConnectorItem) -> None:
        src_item = self._node_items.get(item.model.source_id)
        tgt_item = self._node_items.get(item.model.target_id)
        if not src_item or not tgt_item:
            return

        tgt_center = tgt_item.mapToScene(
            QPointF(tgt_item._width / 2, tgt_item._height / 2)
        )
        src_center = src_item.mapToScene(
            QPointF(src_item._width / 2, src_item._height / 2)
        )

        src_anchor = src_item.get_nearest_anchor(tgt_center)
        tgt_anchor = tgt_item.get_nearest_anchor(src_center)
        item.set_endpoints(src_anchor, tgt_anchor)

    def _update_scene_rect(self) -> None:
        items_rect = self.itemsBoundingRect()
        if items_rect.isNull():
            return
        margin = 200
        expanded = items_rect.adjusted(-margin, -margin, margin, margin)
        current = self.sceneRect()
        if not current.contains(expanded):
            self.setSceneRect(current.united(expanded))
