from __future__ import annotations

from PySide6.QtCore import QMimeData, QPointF, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QGraphicsView

from ..model.node_model import NodeModel
from ..model.shape_types import ShapeType
from .diagram_scene import DiagramScene

SHAPE_MIME_TYPE = "application/x-sharkdrown-shape"


class DiagramView(QGraphicsView):
    """Graphics view with zoom (Ctrl+scroll), pan (middle-click), and drop support."""

    zoom_changed = Signal(float)

    def __init__(self, scene: DiagramScene, parent=None) -> None:
        super().__init__(scene, parent)
        self._zoom = 1.0
        self._panning = False
        self._pan_start = QPointF()

        self.setRenderHints(
            QPainter.Antialiasing
            | QPainter.SmoothPixmapTransform
            | QPainter.TextAntialiasing
        )
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    @property
    def zoom_level(self) -> float:
        return self._zoom

    def set_zoom(self, factor: float) -> None:
        factor = max(0.1, min(5.0, factor))
        scale_by = factor / self._zoom
        self._zoom = factor
        self.scale(scale_by, scale_by)
        self.zoom_changed.emit(self._zoom)

    def zoom_to_fit(self) -> None:
        scene = self.scene()
        if scene is None:
            return
        items_rect = scene.itemsBoundingRect()
        if items_rect.isNull():
            return
        self.fitInView(items_rect.adjusted(-50, -50, 50, 50), Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 1 / 1.15
            new_zoom = self._zoom * factor
            self.set_zoom(new_zoom)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(SHAPE_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(SHAPE_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        if not mime.hasFormat(SHAPE_MIME_TYPE):
            super().dropEvent(event)
            return

        shape_value = bytes(mime.data(SHAPE_MIME_TYPE)).decode("utf-8")
        try:
            shape_type = ShapeType(shape_value)
        except ValueError:
            event.ignore()
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        node = NodeModel(
            shape_type=shape_type,
            label=shape_type.display_name,
            x=scene_pos.x() - 60,
            y=scene_pos.y() - 30,
        )

        scene = self.scene()
        if isinstance(scene, DiagramScene):
            scene.add_node(node)

        event.acceptProposedAction()
