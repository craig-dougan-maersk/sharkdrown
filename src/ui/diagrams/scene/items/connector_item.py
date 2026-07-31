from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsTextItem,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ...model.connector_model import ConnectorModel
from ...model.connector_types import EndingType, LineStyle


class ConnectorItem(QGraphicsPathItem):
    """Visual connector (edge) between two diagram nodes.

    Draws a path from source anchor to target anchor with configurable
    line style and endpoint markers (arrow, circle, cross).
    """

    def __init__(self, model: ConnectorModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._source_point = QPointF(0, 0)
        self._target_point = QPointF(0, 0)

        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setZValue(-1)

        self._label_item = QGraphicsTextItem(self)
        self._label_item.setPlainText(model.label)
        self._label_item.setDefaultTextColor(QColor("#555555"))

        self._update_pen()

    @property
    def model(self) -> ConnectorModel:
        return self._model

    @property
    def connector_id(self) -> str:
        return self._model.id

    def set_endpoints(self, source: QPointF, target: QPointF) -> None:
        self._source_point = source
        self._target_point = target
        self._rebuild_path()

    def set_label(self, text: str) -> None:
        self._model.label = text
        self._label_item.setPlainText(text)
        self._position_label()

    def _update_pen(self) -> None:
        pen = QPen(QColor("#555555"), 2.0)
        if self._model.line_style == LineStyle.DOTTED:
            pen.setStyle(Qt.DashLine)
        elif self._model.line_style == LineStyle.THICK:
            pen.setWidthF(3.5)
        self.setPen(pen)

    def _rebuild_path(self) -> None:
        path = QPainterPath()
        path.moveTo(self._source_point)

        dx = abs(self._target_point.x() - self._source_point.x())
        dy = abs(self._target_point.y() - self._source_point.y())

        if dx > 20 and dy > 20:
            mid_y = (self._source_point.y() + self._target_point.y()) / 2
            path.lineTo(self._source_point.x(), mid_y)
            path.lineTo(self._target_point.x(), mid_y)
        path.lineTo(self._target_point)

        self.setPath(path)
        self._position_label()

    def _position_label(self) -> None:
        if not self._model.label:
            self._label_item.setVisible(False)
            return
        self._label_item.setVisible(True)
        mid = QPointF(
            (self._source_point.x() + self._target_point.x()) / 2,
            (self._source_point.y() + self._target_point.y()) / 2,
        )
        br = self._label_item.boundingRect()
        self._label_item.setPos(mid.x() - br.width() / 2, mid.y() - br.height() - 4)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        super().paint(painter, option, widget)

        if self._model.end_ending != EndingType.OPEN:
            self._draw_ending(painter, self._target_point, self._model.end_ending, True)
        if self._model.start_ending != EndingType.OPEN:
            self._draw_ending(
                painter, self._source_point, self._model.start_ending, False
            )

    def _draw_ending(
        self,
        painter: QPainter,
        point: QPointF,
        ending: EndingType,
        is_target: bool,
    ) -> None:
        if is_target:
            other = self._source_point
        else:
            other = self._target_point

        angle = math.atan2(point.y() - other.y(), point.x() - other.x())
        size = 10.0

        painter.save()
        pen_color = self.pen().color()
        painter.setPen(QPen(pen_color, 2.0))

        if ending == EndingType.ARROW:
            p1 = QPointF(
                point.x() - size * math.cos(angle - 0.4),
                point.y() - size * math.sin(angle - 0.4),
            )
            p2 = QPointF(
                point.x() - size * math.cos(angle + 0.4),
                point.y() - size * math.sin(angle + 0.4),
            )
            painter.setBrush(pen_color)
            painter.drawPolygon(QPolygonF([point, p1, p2]))
        elif ending == EndingType.CIRCLE:
            cx = point.x() - (size / 2) * math.cos(angle)
            cy = point.y() - (size / 2) * math.sin(angle)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), size / 2, size / 2)
        elif ending == EndingType.CROSS:
            painter.drawLine(
                point + QPointF(-size / 2, -size / 2),
                point + QPointF(size / 2, size / 2),
            )
            painter.drawLine(
                point + QPointF(-size / 2, size / 2),
                point + QPointF(size / 2, -size / 2),
            )

        painter.restore()
