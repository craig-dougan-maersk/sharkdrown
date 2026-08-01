from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
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
from .anchor_point import ANCHOR_SOURCE, ANCHOR_TARGET, AnchorPoint


class ConnectorItem(QGraphicsPathItem):
    """Visual connector (edge) between two diagram nodes.

    Draws a path from source anchor to target anchor with configurable
    line style, endpoint markers, color, and weight. When selected, two
    endpoint handles are revealed so the user can re-route either end.

    Note: ``QGraphicsPathItem`` is not a ``QObject`` subclass, so we use a
    Python callback (``set_label_listener``) instead of a Qt ``Signal``.
    """

    def __init__(self, model: ConnectorModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._source_point = QPointF(0, 0)
        self._target_point = QPointF(0, 0)
        self._label_listener: callable | None = None

        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setZValue(-1)

        self._label_item = QGraphicsTextItem(self)
        self._label_item.setPlainText(model.label)
        self._label_item.setDefaultTextColor(QColor("#555555"))
        self._label_item.setTextInteractionFlags(Qt.NoTextInteraction)

        # Endpoint handles (visible only when this connector is selected).
        self._source_handle = AnchorPoint(QPointF(0, 0), self, role=ANCHOR_SOURCE)
        self._target_handle = AnchorPoint(QPointF(0, 0), self, role=ANCHOR_TARGET)
        self._source_handle.hide_anchor()
        self._target_handle.hide_anchor()

        self._update_pen()

    def set_label_listener(self, listener: callable | None) -> None:
        """Register a callback to be invoked when label text is committed.

        Called as ``listener(connector_id, new_text)``. Used because
        ``QGraphicsPathItem`` is not a ``QObject`` and cannot declare
        ``Signal`` instances.
        """
        self._label_listener = listener

    @property
    def model(self) -> ConnectorModel:
        return self._model

    @property
    def connector_id(self) -> str:
        return self._model.id

    @property
    def source_handle(self) -> AnchorPoint:
        return self._source_handle

    @property
    def target_handle(self) -> AnchorPoint:
        return self._target_handle

    def set_endpoints(self, source: QPointF, target: QPointF) -> None:
        self._source_point = source
        self._target_point = target
        self._source_handle.setPos(self.mapFromScene(source))
        self._target_handle.setPos(self.mapFromScene(target))
        self._rebuild_path()

    def set_label(self, text: str) -> None:
        self._model.label = text
        self._label_item.setPlainText(text)
        self._position_label()

    def set_color(self, color: str) -> None:
        self._model.color = color
        self._update_pen()
        self.update()

    def set_weight(self, weight: float) -> None:
        self._model.weight = weight
        self._update_pen()
        self.update()

    def set_line_style(self, line_style: LineStyle) -> None:
        self._model.line_style = line_style
        self._update_pen()
        self.update()

    def set_start_ending(self, ending: EndingType) -> None:
        self._model.start_ending = ending
        self.update()

    def set_end_ending(self, ending: EndingType) -> None:
        self._model.end_ending = ending
        self.update()

    def _update_pen(self) -> None:
        pen = QPen(QColor(self._model.color), self._model.weight)
        if self._model.line_style == LineStyle.DOTTED:
            pen.setStyle(Qt.DashLine)
        elif self._model.line_style == LineStyle.THICK:
            pen.setWidthF(self._model.weight * 1.75)
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

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedChange:
            show = bool(value)
            # Defer visibility toggle so the parent map is settled.
            QTimer.singleShot(0, lambda s=show: (
                self._source_handle.setVisible(s),
                self._target_handle.setVisible(s),
            ))
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:
        self._label_item.setTextInteractionFlags(Qt.TextEditorInteraction)
        self._label_item.setFocus()
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event) -> None:
        self._label_item.setTextInteractionFlags(Qt.NoTextInteraction)
        new_text = self._label_item.toPlainText()
        if new_text != self._model.label and self._label_listener is not None:
            self._model.label = new_text
            self._label_listener(self._model.id, new_text)
        self._position_label()
        super().focusOutEvent(event)
