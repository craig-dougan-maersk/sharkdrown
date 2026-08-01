from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsTextItem,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ...model.node_model import NodeModel
from ...model.shape_types import ShapeType
from .anchor_point import ANCHOR_NEW, AnchorPoint


class DiagramNodeItem(QGraphicsObject):
    """Visual representation of a diagram node on the canvas.

    Paints all 14 Mermaid flowchart shapes via QPainterPath. Movable,
    selectable, and supports inline text editing via double-click.
    """

    position_changed = Signal(str, float, float)
    size_changed = Signal(str, float, float)

    def __init__(self, model: NodeModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._width = model.width
        self._height = model.height

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)

        self.setPos(model.x, model.y)

        self._text_item = QGraphicsTextItem(self)
        self._text_item.setPlainText(model.label)
        self._text_item.setDefaultTextColor(QColor("#333333"))
        self._center_text()

        self._hovered = False
        self._anchors: list[AnchorPoint] = []

    @property
    def model(self) -> NodeModel:
        return self._model

    @property
    def node_id(self) -> str:
        return self._model.id

    def boundingRect(self) -> QRectF:
        pad = 2.0
        return QRectF(-pad, -pad, self._width + 2 * pad, self._height + 2 * pad)

    def shape(self) -> QPainterPath:
        return self._build_shape_path()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        path = self._build_shape_path()

        fill = QColor(self._model.fill_color)
        outline = QColor(self._model.outline_color)

        painter.setBrush(QBrush(fill))

        pen = QPen(outline, 2.0)
        if self.isSelected():
            pen = QPen(QColor("#0078d4"), 2.5)
        painter.setPen(pen)
        painter.drawPath(path)

    def _build_shape_path(self) -> QPainterPath:
        w, h = self._width, self._height
        path = QPainterPath()
        st = self._model.shape_type

        if st == ShapeType.RECTANGLE:
            path.addRect(0, 0, w, h)
        elif st == ShapeType.ROUNDED_RECT:
            path.addRoundedRect(0, 0, w, h, 10, 10)
        elif st == ShapeType.STADIUM:
            r = h / 2
            path.addRoundedRect(0, 0, w, h, r, r)
        elif st == ShapeType.SUBROUTINE:
            inset = 8
            path.addRect(0, 0, w, h)
            path.moveTo(inset, 0)
            path.lineTo(inset, h)
            path.moveTo(w - inset, 0)
            path.lineTo(w - inset, h)
        elif st == ShapeType.CYLINDRICAL:
            ry = 8
            path.moveTo(0, ry)
            path.arcTo(0, 0, w, 2 * ry, 180, 180)
            path.lineTo(w, h - ry)
            path.arcTo(0, h - 2 * ry, w, 2 * ry, 0, 180)
            path.lineTo(0, ry)
            path.moveTo(0, ry)
            path.arcTo(0, 0, w, 2 * ry, 180, -180)
        elif st == ShapeType.CIRCLE:
            r = min(w, h) / 2
            cx, cy = w / 2, h / 2
            path.addEllipse(QPointF(cx, cy), r, r)
        elif st == ShapeType.DOUBLE_CIRCLE:
            r = min(w, h) / 2
            cx, cy = w / 2, h / 2
            path.addEllipse(QPointF(cx, cy), r, r)
            path.addEllipse(QPointF(cx, cy), r - 5, r - 5)
        elif st == ShapeType.ASYMMETRIC:
            flag_indent = 10
            path.moveTo(flag_indent, 0)
            path.lineTo(w, 0)
            path.lineTo(w - flag_indent, h / 2)
            path.lineTo(w, h)
            path.lineTo(flag_indent, h)
            path.lineTo(0, h / 2)
            path.closeSubpath()
        elif st == ShapeType.RHOMBUS:
            cx, cy = w / 2, h / 2
            path.moveTo(cx, 0)
            path.lineTo(w, cy)
            path.lineTo(cx, h)
            path.lineTo(0, cy)
            path.closeSubpath()
        elif st == ShapeType.HEXAGON:
            inset = w * 0.15
            path.moveTo(inset, 0)
            path.lineTo(w - inset, 0)
            path.lineTo(w, h / 2)
            path.lineTo(w - inset, h)
            path.lineTo(inset, h)
            path.lineTo(0, h / 2)
            path.closeSubpath()
        elif st == ShapeType.PARALLELOGRAM:
            skew = w * 0.15
            path.moveTo(skew, 0)
            path.lineTo(w, 0)
            path.lineTo(w - skew, h)
            path.lineTo(0, h)
            path.closeSubpath()
        elif st == ShapeType.PARALLELOGRAM_ALT:
            skew = w * 0.15
            path.moveTo(0, 0)
            path.lineTo(w - skew, 0)
            path.lineTo(w, h)
            path.lineTo(skew, h)
            path.closeSubpath()
        elif st == ShapeType.TRAPEZOID:
            inset = w * 0.15
            path.moveTo(inset, 0)
            path.lineTo(w - inset, 0)
            path.lineTo(w, h)
            path.lineTo(0, h)
            path.closeSubpath()
        elif st == ShapeType.TRAPEZOID_ALT:
            inset = w * 0.15
            path.moveTo(0, 0)
            path.lineTo(w, 0)
            path.lineTo(w - inset, h)
            path.lineTo(inset, h)
            path.closeSubpath()

        return path

    def _center_text(self) -> None:
        br = self._text_item.boundingRect()
        tx = (self._width - br.width()) / 2
        ty = (self._height - br.height()) / 2
        self._text_item.setPos(tx, ty)

    def set_label(self, text: str) -> None:
        self._model.label = text
        self._text_item.setPlainText(text)
        self._center_text()

    def set_fill_color(self, color: str) -> None:
        self._model.fill_color = color
        self.update()

    def set_outline_color(self, color: str) -> None:
        self._model.outline_color = color
        self.update()

    def get_anchor_points(self) -> list[QPointF]:
        """Return connection anchor positions in scene coordinates."""
        w, h = self._width, self._height
        local_anchors = [
            QPointF(w / 2, 0),       # top
            QPointF(w, h / 2),       # right
            QPointF(w / 2, h),       # bottom
            QPointF(0, h / 2),       # left
        ]
        return [self.mapToScene(p) for p in local_anchors]

    def get_nearest_anchor(self, scene_point: QPointF) -> QPointF:
        """Find the anchor point closest to a given scene coordinate."""
        anchors = self.get_anchor_points()
        return min(anchors, key=lambda a: _distance(a, scene_point))

    def ensure_connection_anchors(self) -> list[AnchorPoint]:
        """Idempotently create 4 AnchorPoints (top/right/bottom/left)."""
        if self._anchors:
            return self._anchors
        for local in (
            QPointF(self._width / 2, 0),  # top
            QPointF(self._width, self._height / 2),  # right
            QPointF(self._width / 2, self._height),  # bottom
            QPointF(0, self._height / 2),  # left
        ):
            anchor = AnchorPoint(local, self, role=ANCHOR_NEW)
            self._anchors.append(anchor)
        return self._anchors

    def show_connection_anchors(self) -> None:
        for anchor in self.ensure_connection_anchors():
            anchor.show_anchor()

    def hide_connection_anchors(self) -> None:
        for anchor in self._anchors:
            anchor.hide_anchor()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            pos = value
            self._model.x = pos.x()
            self._model.y = pos.y()
            self.position_changed.emit(self._model.id, pos.x(), pos.y())
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self._text_item.setTextInteractionFlags(Qt.TextEditorInteraction)
        self._text_item.setFocus()
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event) -> None:
        self._text_item.setTextInteractionFlags(Qt.NoTextInteraction)
        self._model.label = self._text_item.toPlainText()
        self._center_text()
        super().focusOutEvent(event)


def _distance(a: QPointF, b: QPointF) -> float:
    dx = a.x() - b.x()
    dy = a.y() - b.y()
    return math.sqrt(dx * dx + dy * dy)
