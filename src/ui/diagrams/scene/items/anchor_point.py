from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsItem


ANCHOR_SOURCE = "source"
ANCHOR_TARGET = "target"
ANCHOR_NEW = "new"


class AnchorPoint(QGraphicsEllipseItem):
    """Small circle that represents a connection point on a shape or connector.

    Acts as a draggable handle. Emits scene-coordinate positions via
    `drag_started`, `drag_moved`, and `drag_finished` signals. The hit-test
    rect is larger than the visible ellipse so the handle is easy to grab.
    """

    RADIUS = 5.0
    HIT_RADIUS = 10.0

    drag_started = Signal(QPointF)
    drag_moved = Signal(QPointF)
    drag_finished = Signal(QPointF)

    def __init__(
        self,
        local_pos: QPointF,
        parent_item: QGraphicsItem,
        role: str = ANCHOR_NEW,
    ) -> None:
        r = self.HIT_RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r, parent_item)
        self._role = role
        self.setPos(local_pos)
        self.setBrush(QBrush(QColor("#0078d4")))
        self.setPen(QPen(QColor("#005a9e"), 1.0))
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CrossCursor)
        self.setVisible(False)
        self.setZValue(10)

    @property
    def role(self) -> str:
        return self._role

    def paint(self, painter, option, widget=None) -> None:
        # Draw only the inner RADIUS inside the larger HIT_RADIUS rect.
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self.brush())
        painter.setPen(self.pen())
        painter.drawEllipse(QPointF(0, 0), self.RADIUS, self.RADIUS)

    def show_anchor(self) -> None:
        self.setVisible(True)

    def hide_anchor(self) -> None:
        self.setVisible(False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.drag_started.emit(self.mapToScene(QPointF(0, 0)))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self.drag_moved.emit(self.mapToScene(event.pos()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.drag_finished.emit(self.mapToScene(event.pos()))
            event.accept()
            return
        super().mouseReleaseEvent(event)
