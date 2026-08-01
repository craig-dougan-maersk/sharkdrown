from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QMimeData, QPointF, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QGraphicsScene

from ..model.connector_model import ConnectorModel
from ..model.diagram_document import DiagramDocument
from ..model.free_text_model import FreeTextModel
from ..model.node_model import NodeModel
from .items.anchor_point import AnchorPoint
from .items.connector_item import ConnectorItem
from .items.free_text_item import FreeTextItem
from .items.node_item import DiagramNodeItem

CLIPBOARD_MIME = "application/x-sharkdrown-diagram-items"


@dataclass
class _ConnectorCreation:
    """Tracks in-flight connector-creation state on the scene."""

    source_node_id: str = ""
    source_anchor: QPointF = field(default_factory=QPointF)
    temp_item: ConnectorItem | None = None
    active: bool = False


class DiagramScene(QGraphicsScene):
    """Graphics scene that syncs with a DiagramDocument model.

    All mutations go through the document first, then the scene updates
    its visual items to match. Emits `document_changed` after any mutation
    so the code preview can regenerate.
    """

    document_changed = Signal()
    connector_label_edited = Signal(str, str)  # (connector_id, new_text)

    def __init__(self, document: DiagramDocument | None = None, parent=None) -> None:
        super().__init__(parent)
        self._document = document or DiagramDocument()
        self._node_items: dict[str, DiagramNodeItem] = {}
        self._connector_items: dict[str, ConnectorItem] = {}
        self._text_items: dict[str, FreeTextItem] = {}

        self.setSceneRect(-2000, -2000, 4000, 4000)

        self._connector_mode = False
        self._creation = _ConnectorCreation()
        self._dragging_endpoint: AnchorPoint | None = None

        self.selectionChanged.connect(self._on_selection_changed)

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
        self._creation = _ConnectorCreation()
        self._dragging_endpoint = None
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

    def is_connector_mode(self) -> bool:
        return self._connector_mode

    def set_connector_mode(self, on: bool) -> None:
        self._connector_mode = on
        if not on:
            self._cancel_creation()

    def handle_left_click(self, scene_pos: QPointF, item) -> bool:
        """Handle a left-click at the given scene position.

        Returns True if the scene captured the click (used by the view to
        accept() the event and prevent further propagation).
        """
        # Case 1: starting connector creation. Either explicit connector mode,
        # or a click on a node anchor (a shape-side connection point).
        if item is not None:
            anchor = item if isinstance(item, AnchorPoint) else None
            if anchor is None:
                # Walk up the parent chain to find an AnchorPoint.
                cur = item.parentItem()
                while cur is not None and not isinstance(cur, AnchorPoint):
                    cur = cur.parentItem()
                anchor = cur if isinstance(cur, AnchorPoint) else None

            if anchor is not None and isinstance(anchor.parentItem(), DiagramNodeItem):
                # Anchors on shapes always start a drag (regardless of mode).
                self._start_connector_creation(anchor.parentItem(), scene_pos)
                return True

        if self._connector_mode:
            node = self._node_for_item(item)
            if node is not None:
                self._start_connector_creation(node, scene_pos)
                return True

        # Case 2: clicking an endpoint handle of an existing connector.
        if isinstance(item, AnchorPoint) and isinstance(
            item.parentItem(), ConnectorItem
        ):
            self._begin_endpoint_drag(item)
            return True

        return False

    def handle_mouse_move(self, scene_pos: QPointF) -> None:
        if self._creation.active and self._creation.temp_item is not None:
            anchor = self._creation.source_anchor
            self._creation.temp_item.set_endpoints(anchor, scene_pos)
        elif self._dragging_endpoint is not None:
            self._update_endpoint_drag_preview(scene_pos)

    def handle_mouse_release(self, scene_pos: QPointF, item) -> bool:
        if self._creation.active:
            target_node = self._node_for_item(item)
            if (
                target_node is not None
                and target_node.node_id != self._creation.source_node_id
            ):
                self._complete_connector_creation(target_node.node_id)
            else:
                self._cancel_creation()
            return True

        if self._dragging_endpoint is not None:
            target_node = self._node_for_item(item)
            self._complete_endpoint_drag(self._dragging_endpoint, target_node)
            self._dragging_endpoint = None
            return True

        return False

    def cancel_creation(self) -> None:
        self._cancel_creation()

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
        item.set_label_listener(self._on_connector_label_changed)
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

    def _on_connector_label_changed(self, connector_id: str, new_text: str) -> None:
        self.connector_label_edited.emit(connector_id, new_text)
        self.document_changed.emit()

    def _on_selection_changed(self) -> None:
        selected_nodes = {
            it for it in self.selectedItems() if isinstance(it, DiagramNodeItem)
        }
        for node_item in self._node_items.values():
            if node_item in selected_nodes:
                node_item.show_connection_anchors()
            else:
                node_item.hide_connection_anchors()

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

    # --- Connector creation -------------------------------------------------

    def _node_for_item(self, item):
        """Walk up the parent chain to find a DiagramNodeItem, if any."""
        cur = item
        while cur is not None:
            if isinstance(cur, DiagramNodeItem):
                return cur
            cur = cur.parentItem()
        return None

    def _start_connector_creation(
        self, source_node: DiagramNodeItem, scene_pt: QPointF
    ) -> None:
        self._cancel_creation()
        self._creation.source_node_id = source_node.node_id
        self._creation.source_anchor = source_node.get_nearest_anchor(scene_pt)
        temp_model = ConnectorModel(
            source_id=source_node.node_id,
            target_id=source_node.node_id,  # placeholder; ignored visually
        )
        self._creation.temp_item = ConnectorItem(temp_model)
        self.addItem(self._creation.temp_item)
        self._creation.active = True

    def _complete_connector_creation(self, target_node_id: str) -> None:
        if not self._creation.active:
            return
        src_id = self._creation.source_node_id
        if src_id == target_node_id:
            self._cancel_creation()
            return

        temp = self._creation.temp_item
        if temp is not None:
            self.removeItem(temp)
        self._creation.temp_item = None
        self._creation.active = False

        # Build the final model with the target attached.
        final_model = ConnectorModel(
            source_id=src_id,
            target_id=target_node_id,
        )
        self.add_connector(final_model)

    def _cancel_creation(self) -> None:
        if self._creation.temp_item is not None:
            self.removeItem(self._creation.temp_item)
        self._creation = _ConnectorCreation()

    # --- Endpoint re-route --------------------------------------------------

    def _begin_endpoint_drag(self, handle: AnchorPoint) -> None:
        self._dragging_endpoint = handle
        parent = handle.parentItem()
        if isinstance(parent, ConnectorItem):
            parent.setSelected(True)

    def _update_endpoint_drag_preview(self, scene_pos: QPointF) -> None:
        handle = self._dragging_endpoint
        parent = handle.parentItem() if handle is not None else None
        if not isinstance(parent, ConnectorItem):
            return
        if handle is self._source_handle_of(parent):
            parent.set_endpoints(scene_pos, parent._target_point)
        else:
            parent.set_endpoints(parent._source_point, scene_pos)

    def _source_handle_of(self, connector: ConnectorItem) -> AnchorPoint:
        return connector.source_handle

    def _complete_endpoint_drag(
        self, handle: AnchorPoint, target_node: DiagramNodeItem | None
    ) -> None:
        parent = handle.parentItem()
        if not isinstance(parent, ConnectorItem):
            return
        model = parent.model
        is_source = handle is parent.source_handle
        current_id = model.source_id if is_source else model.target_id
        if target_node is not None and target_node.node_id != current_id:
            if is_source:
                new_model = ConnectorModel(
                    id=model.id,
                    source_id=target_node.node_id,
                    target_id=model.target_id,
                    line_style=model.line_style,
                    start_ending=model.start_ending,
                    end_ending=model.end_ending,
                    label=model.label,
                    color=model.color,
                    weight=model.weight,
                )
            else:
                new_model = ConnectorModel(
                    id=model.id,
                    source_id=model.source_id,
                    target_id=target_node.node_id,
                    line_style=model.line_style,
                    start_ending=model.start_ending,
                    end_ending=model.end_ending,
                    label=model.label,
                    color=model.color,
                    weight=model.weight,
                )
            self._document.update_connector(new_model)
            self._update_connector_endpoints(parent)
            self.document_changed.emit()
        else:
            # Snap back to original anchors.
            self._update_connector_endpoints(parent)
