"""Categorised shape palette for drag-and-drop onto the diagram canvas."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData, Qt
from PySide6.QtGui import QDrag, QMouseEvent, QPainter, QPainterPath, QPixmap, QColor, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..model.shape_types import ShapeType
from ..scene.diagram_view import SHAPE_MIME_TYPE


class PaletteItem(QFrame):
    """A single draggable shape thumbnail in the palette."""

    def __init__(self, shape_type: ShapeType, parent=None) -> None:
        super().__init__(parent)
        self._shape_type = shape_type
        self.setFixedSize(72, 56)
        self.setFrameStyle(QFrame.Box | QFrame.Plain)
        self.setLineWidth(1)
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip(shape_type.display_name)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(1)

        self._icon = ShapeIcon(shape_type)
        self._icon.setFixedSize(64, 32)
        layout.addWidget(self._icon, alignment=Qt.AlignCenter)

        label = QLabel(shape_type.display_name)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("font-size: 9px; color: #666;")
        layout.addWidget(label)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._start_drag()

    def _start_drag(self) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(SHAPE_MIME_TYPE, QByteArray(self._shape_type.value.encode("utf-8")))
        drag.setMimeData(mime)

        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.transparent)
        self.render(pixmap)
        drag.setPixmap(pixmap)

        drag.exec(Qt.CopyAction)


class ShapeIcon(QWidget):
    """Mini preview of a shape type drawn via QPainterPath."""

    def __init__(self, shape_type: ShapeType, parent=None) -> None:
        super().__init__(parent)
        self._shape_type = shape_type

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#555555"), 1.5))
        painter.setBrush(QColor("#e8f0fe"))

        w = self.width() - 4
        h = self.height() - 4
        painter.translate(2, 2)

        path = self._build_mini_path(w, h)
        painter.drawPath(path)
        painter.end()

    def _build_mini_path(self, w: float, h: float) -> QPainterPath:
        from ..scene.items.node_item import DiagramNodeItem
        from ..model.node_model import NodeModel

        temp_model = NodeModel(shape_type=self._shape_type, width=w, height=h)
        temp_item = DiagramNodeItem(temp_model)
        path = temp_item._build_shape_path()
        return path


class ShapePalette(QScrollArea):
    """Scrollable palette with shape categories."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameStyle(QFrame.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        categories: dict[str, list[ShapeType]] = {}
        for shape in ShapeType:
            cat = shape.category
            categories.setdefault(cat, []).append(shape)

        for cat_name in ["Basic", "Flowchart", "Process"]:
            shapes = categories.get(cat_name, [])
            if not shapes:
                continue

            header = QLabel(f"<b>{cat_name}</b>")
            header.setStyleSheet("font-size: 11px; color: #333; padding: 2px 0;")
            layout.addWidget(header)

            grid = QGridLayout()
            grid.setSpacing(4)
            for i, shape in enumerate(shapes):
                item = PaletteItem(shape)
                grid.addWidget(item, i // 2, i % 2)
            layout.addLayout(grid)

        layout.addStretch()
        self.setWidget(container)
