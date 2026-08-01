"""Drop-down drawer for connector style controls."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..model.connector_model import ConnectorModel
from ..model.connector_types import EndingType, LineStyle


_LINE_STYLE_LABELS = [
    (LineStyle.SOLID, "Solid"),
    (LineStyle.DOTTED, "Dotted"),
    (LineStyle.THICK, "Thick"),
]

_ENDING_LABELS = [
    (EndingType.OPEN, "Open"),
    (EndingType.ARROW, "Arrow"),
    (EndingType.CIRCLE, "Circle"),
    (EndingType.CROSS, "Cross"),
]


class ConnectorStyleDrawer(QWidget):
    """Drop-down drawer with style controls for selected connector(s).

    Always shows a 'Connector Style' toggle button. When enabled, the user
    can open the panel to adjust line style, weight, colour, endings, and
    label. Controls are disabled when no connector is selected.
    """

    line_style_changed = Signal(str)
    weight_changed = Signal(float)
    color_changed = Signal(str)
    start_ending_changed = Signal(str)
    end_ending_changed = Signal(str)
    label_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.set_enabled(False)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setText("Connector Style ▾")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setToolTip("Show connector style controls")
        self._toggle_btn.setEnabled(False)
        self._toggle_btn.toggled.connect(self._on_toggled)
        layout.addWidget(self._toggle_btn)

        self._panel = QFrame()
        self._panel.setFrameShape(QFrame.StyledPanel)
        self._panel.setVisible(False)
        self._panel.setEnabled(False)

        form = QFormLayout(self._panel)
        form.setContentsMargins(8, 4, 8, 4)
        form.setSpacing(4)

        # Line style
        self._line_style_combo = QComboBox()
        for style, label in _LINE_STYLE_LABELS:
            self._line_style_combo.addItem(label, style.value)
        self._line_style_combo.currentIndexChanged.connect(
            lambda _i: self.line_style_changed.emit(
                self._line_style_combo.currentData()
            )
        )
        form.addRow("Line Style", self._line_style_combo)

        # Weight
        self._weight_slider = QSlider(Qt.Horizontal)
        self._weight_slider.setRange(10, 80)
        self._weight_slider.setValue(20)
        self._weight_slider.setFixedWidth(120)
        self._weight_slider.valueChanged.connect(
            lambda v: self.weight_changed.emit(v / 10.0)
        )
        form.addRow("Weight", self._weight_slider)

        # Colour
        self._color_btn = QToolButton()
        self._color_btn.setText("Pick…")
        self._color_btn.clicked.connect(self._pick_color)
        form.addRow("Colour", self._color_btn)

        # Endings
        self._start_ending_combo = QComboBox()
        for ending, label in _ENDING_LABELS:
            self._start_ending_combo.addItem(label, ending.value)
        self._start_ending_combo.currentIndexChanged.connect(
            lambda _i: self.start_ending_changed.emit(
                self._start_ending_combo.currentData()
            )
        )
        form.addRow("Start", self._start_ending_combo)

        self._end_ending_combo = QComboBox()
        for ending, label in _ENDING_LABELS:
            self._end_ending_combo.addItem(label, ending.value)
        # Default to Arrow for the end.
        self._end_ending_combo.setCurrentIndex(1)
        self._end_ending_combo.currentIndexChanged.connect(
            lambda _i: self.end_ending_changed.emit(
                self._end_ending_combo.currentData()
            )
        )
        form.addRow("End", self._end_ending_combo)

        # Label
        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("Connector label")
        self._label_edit.editingFinished.connect(
            lambda: self.label_changed.emit(self._label_edit.text())
        )
        form.addRow("Label", self._label_edit)

        layout.addWidget(self._panel)
        layout.addStretch(1)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the drawer (and collapse any open panel)."""
        self._toggle_btn.setEnabled(enabled)
        if not enabled:
            self._toggle_btn.setChecked(False)
            self._panel.setVisible(False)
        self._panel.setEnabled(enabled)

    def populate(self, model: ConnectorModel) -> None:
        """Preload the controls from a connector model."""
        # Block signals so we don't re-emit while loading.
        for combo, value in (
            (self._line_style_combo, model.line_style.value),
            (self._start_ending_combo, model.start_ending.value),
            (self._end_ending_combo, model.end_ending.value),
        ):
            idx = combo.findData(value)
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        self._weight_slider.blockSignals(True)
        self._weight_slider.setValue(int(round(model.weight * 10)))
        self._weight_slider.blockSignals(False)
        self._label_edit.blockSignals(True)
        self._label_edit.setText(model.label)
        self._label_edit.blockSignals(False)
        self._color_btn.setText(model.color)
        # Store the colour on the button itself so _pick_color can read it.
        self._color_btn.setProperty("current_color", model.color)

    def set_color(self, color: str) -> None:
        """Update the colour button label without triggering a re-pick."""
        self._color_btn.blockSignals(True)
        self._color_btn.setText(color)
        self._color_btn.setProperty("current_color", color)
        self._color_btn.blockSignals(False)

    def _on_toggled(self, checked: bool) -> None:
        self._panel.setVisible(checked)

    def _pick_color(self) -> None:
        current = self._color_btn.property("current_color") or "#555555"
        color = QColorDialog.getColor(QColor(current), self, "Connector Colour")
        if color.isValid():
            self.color_changed.emit(color.name())
