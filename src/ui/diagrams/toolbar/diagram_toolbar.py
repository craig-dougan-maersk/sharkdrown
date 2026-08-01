"""Diagram editor toolbar — zoom, undo/redo, connector mode, colour."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QLabel,
    QSlider,
    QToolBar,
    QToolButton,
    QWidget,
)

from .connector_style_drawer import ConnectorStyleDrawer


class DiagramToolbar(QToolBar):
    """Toolbar for the diagram editor canvas."""

    connector_mode_toggled = Signal(bool)
    zoom_changed = Signal(float)
    zoom_to_fit_requested = Signal()
    undo_requested = Signal()
    redo_requested = Signal()
    delete_requested = Signal()
    fill_color_picked = Signal(str)
    outline_color_picked = Signal(str)
    add_text_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__("Diagram Tools", parent)
        self.setMovable(False)
        self.setIconSize(self.iconSize())

        self._connector_mode = False

        self._build_zoom_controls()
        self.addSeparator()
        self._build_edit_controls()
        self.addSeparator()
        self._build_connector_toggle()
        self.addSeparator()
        self._build_color_controls()
        self.addSeparator()
        self._build_connector_style_drawer()
        self.addSeparator()
        self._build_text_button()

    def _build_zoom_controls(self) -> None:
        self.addWidget(QLabel(" Zoom: "))

        self._zoom_slider = QSlider(Qt.Horizontal)
        self._zoom_slider.setRange(10, 500)
        self._zoom_slider.setValue(100)
        self._zoom_slider.setFixedWidth(120)
        self._zoom_slider.valueChanged.connect(
            lambda v: self.zoom_changed.emit(v / 100.0)
        )
        self.addWidget(self._zoom_slider)

        self._zoom_label = QLabel("100%")
        self._zoom_label.setFixedWidth(45)
        self.addWidget(self._zoom_label)

        fit_action = QAction("Fit", self)
        fit_action.setToolTip("Zoom to fit all items")
        fit_action.triggered.connect(self.zoom_to_fit_requested.emit)
        self.addAction(fit_action)

    def _build_edit_controls(self) -> None:
        undo_action = QAction("Undo", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.triggered.connect(self.undo_requested.emit)
        self.addAction(undo_action)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut("Ctrl+Shift+Z")
        redo_action.triggered.connect(self.redo_requested.emit)
        self.addAction(redo_action)

        delete_action = QAction("Delete", self)
        delete_action.setShortcut("Delete")
        delete_action.triggered.connect(self.delete_requested.emit)
        self.addAction(delete_action)

    def _build_connector_toggle(self) -> None:
        self._connector_btn = QToolButton()
        self._connector_btn.setText("Connect")
        self._connector_btn.setCheckable(True)
        self._connector_btn.setToolTip("Toggle connector drawing mode")
        self._connector_btn.toggled.connect(self._on_connector_toggled)
        self.addWidget(self._connector_btn)

    def _build_color_controls(self) -> None:
        self._fill_btn = QToolButton()
        self._fill_btn.setText("Fill")
        self._fill_btn.setToolTip("Set fill colour for selected shapes")
        self._fill_btn.clicked.connect(self._pick_fill_color)
        self.addWidget(self._fill_btn)

        self._outline_btn = QToolButton()
        self._outline_btn.setText("Outline")
        self._outline_btn.setToolTip("Set outline colour for selected shapes")
        self._outline_btn.clicked.connect(self._pick_outline_color)
        self.addWidget(self._outline_btn)

    def _build_text_button(self) -> None:
        text_btn = QToolButton()
        text_btn.setText("Text")
        text_btn.setToolTip("Add a free text annotation")
        text_btn.clicked.connect(self.add_text_requested.emit)
        self.addWidget(text_btn)

    def _build_connector_style_drawer(self) -> None:
        self._style_drawer = ConnectorStyleDrawer()
        self.addWidget(self._style_drawer)

    def set_connector_selection(self, count: int) -> None:
        """Enable/disable the connector style drawer based on selection."""
        self._style_drawer.set_enabled(count > 0)

    def populate_style_drawer(self, model) -> None:
        """Preload the style drawer from a connector model."""
        self._style_drawer.populate(model)

    def update_zoom_display(self, zoom: float) -> None:
        pct = int(zoom * 100)
        self._zoom_label.setText(f"{pct}%")
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(pct)
        self._zoom_slider.blockSignals(False)

    def _on_connector_toggled(self, checked: bool) -> None:
        self._connector_mode = checked
        self.connector_mode_toggled.emit(checked)

    def _pick_fill_color(self) -> None:
        color = QColorDialog.getColor(QColor("#ffffff"), self, "Fill Colour")
        if color.isValid():
            self.fill_color_picked.emit(color.name())

    def _pick_outline_color(self) -> None:
        color = QColorDialog.getColor(QColor("#333333"), self, "Outline Colour")
        if color.isValid():
            self.outline_color_picked.emit(color.name())
