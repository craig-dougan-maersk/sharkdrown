from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QGraphicsItem, QGraphicsTextItem

from ...model.free_text_model import FreeTextModel


class FreeTextItem(QGraphicsTextItem):
    """Free-floating text annotation on the diagram canvas.

    Double-click to edit; click away to finish editing.
    """

    def __init__(self, model: FreeTextModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model

        self.setPlainText(model.text)
        self.setDefaultTextColor(QColor(model.color))
        self.setFont(QFont("Sans", model.font_size))
        self.setPos(model.x, model.y)

        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)

    @property
    def model(self) -> FreeTextModel:
        return self._model

    @property
    def text_id(self) -> str:
        return self._model.id

    def mouseDoubleClickEvent(self, event) -> None:
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus()
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event) -> None:
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self._model.text = self.toPlainText()
        super().focusOutEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self._model.x = value.x()
            self._model.y = value.y()
        return super().itemChange(change, value)
