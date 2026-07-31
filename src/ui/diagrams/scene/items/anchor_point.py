from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsEllipseItem, QGraphicsItem


class AnchorPoint(QGraphicsEllipseItem):
    """Small circle shown on node hover to indicate connection points.

    Positioned relative to the parent node item. When the user starts
    a drag from an anchor, the scene enters connector-creation mode.
    """

    RADIUS = 5.0

    def __init__(self, local_pos: QPointF, parent_item: QGraphicsItem) -> None:
        r = self.RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r, parent_item)
        self.setPos(local_pos)
        self.setBrush(QBrush(QColor("#0078d4")))
        self.setPen(QPen(QColor("#005a9e"), 1.0))
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CrossCursor)
        self.setVisible(False)
        self.setZValue(10)

    def show_anchor(self) -> None:
        self.setVisible(True)

    def hide_anchor(self) -> None:
        self.setVisible(False)
