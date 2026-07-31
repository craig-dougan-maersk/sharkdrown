"""Mini-map overlay showing an overview of the entire diagram scene."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView


class MiniMap(QGraphicsView):
    """Small overview of the diagram scene, overlaid on the main canvas.

    Shows the full scene extent with a viewport rectangle indicating what
    the main view currently shows. Clicking the minimap scrolls the main view.
    """

    def __init__(
        self, scene: QGraphicsScene, main_view: QGraphicsView, parent=None
    ) -> None:
        super().__init__(scene, parent)
        self._main_view = main_view

        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setInteractive(False)
        self.setStyleSheet(
            "border: 1px solid #ccc; background: rgba(255, 255, 255, 200);"
        )

        self._viewport_rect = QRectF()

    def update_viewport_rect(self) -> None:
        """Recalculate the visible viewport rectangle indicator."""
        visible = self._main_view.mapToScene(
            self._main_view.viewport().rect()
        ).boundingRect()
        self._viewport_rect = visible
        self._fit_scene()
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        if self._viewport_rect.isNull():
            return
        painter.setPen(QPen(QColor("#0078d4"), 1.5))
        painter.setBrush(QBrush(QColor(0, 120, 212, 30)))
        painter.drawRect(self._viewport_rect)

    def mousePressEvent(self, event) -> None:
        scene_pos = self.mapToScene(event.position().toPoint())
        self._main_view.centerOn(scene_pos)
        self.update_viewport_rect()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            self._main_view.centerOn(scene_pos)
            self.update_viewport_rect()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_scene()

    def _fit_scene(self) -> None:
        scene = self.scene()
        if scene is None:
            return
        items_rect = scene.itemsBoundingRect()
        if items_rect.isNull():
            items_rect = scene.sceneRect()
        self.fitInView(items_rect.adjusted(-50, -50, 50, 50), Qt.KeepAspectRatio)
