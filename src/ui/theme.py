"""Unified dark/light theme for the SharkDrown desktop shell.

Provides a QPalette-based theme that affects all Qt widgets uniformly.
The terminal renderer already uses its own dark palette (xterm-256 colors);
this module handles the surrounding chrome (menus, tabs, labels, buttons).

Usage:
    from src.ui.theme import ThemeManager
    theme = ThemeManager()
    theme.apply("dark")   # or "light" or "system"
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


class ThemeMode(str, Enum):
    SYSTEM = "system"
    DARK = "dark"
    LIGHT = "light"


_SETTINGS_KEY = "appearance/theme"


def _dark_palette() -> QPalette:
    """Build a dark QPalette matching common IDE conventions."""
    p = QPalette()
    base = QColor(30, 30, 30)
    window = QColor(45, 45, 45)
    text = QColor(220, 220, 220)
    disabled_text = QColor(127, 127, 127)
    highlight = QColor(42, 130, 218)

    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, window)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.Link, highlight)
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))

    # Disabled state
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)

    return p


class ThemeManager:
    """Manages application-wide theme (dark/light/system).

    Persists the user's choice in QSettings so it survives restarts.
    """

    def __init__(self) -> None:
        self._current: ThemeMode = ThemeMode.SYSTEM
        self._system_palette: QPalette | None = None

    def load_saved(self) -> ThemeMode:
        """Load the persisted theme preference and apply it."""
        settings = QSettings()
        raw = settings.value(_SETTINGS_KEY, ThemeMode.SYSTEM.value)
        try:
            mode = ThemeMode(raw)
        except ValueError:
            mode = ThemeMode.SYSTEM
        self.apply(mode)
        return mode

    def apply(self, mode: ThemeMode) -> None:
        """Apply the theme to the running QApplication."""
        app = QApplication.instance()
        if app is None:
            return

        if self._system_palette is None:
            self._system_palette = QPalette(app.palette())

        self._current = mode

        if mode == ThemeMode.DARK:
            app.setPalette(_dark_palette())
            app.setStyleSheet(
                "QToolTip { color: #dcdcdc; background-color: #323232; "
                "border: 1px solid #555; }"
            )
        elif mode == ThemeMode.LIGHT:
            app.setPalette(self._system_palette)
            app.setStyleSheet("")
        else:
            app.setPalette(self._system_palette)
            app.setStyleSheet("")

        settings = QSettings()
        settings.setValue(_SETTINGS_KEY, mode.value)

    @property
    def current(self) -> ThemeMode:
        return self._current
