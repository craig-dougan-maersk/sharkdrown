"""Unified theme system for the SharkDrown desktop shell.

Provides a QPalette-based theme that affects all Qt widgets uniformly.
The terminal renderer already uses its own dark palette (xterm-256 colors);
this module handles the surrounding chrome (menus, tabs, labels, buttons).

Usage:
    from src.ui.theme import ThemeManager
    theme = ThemeManager()
    theme.apply("dark")   # or "light", "system", "warm", etc.
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
    WARM = "warm"
    COOL = "cool"
    HIGH_CONTRAST = "high contrast"
    YELLOW_ON_BLUE = "yellow on blue"


_SETTINGS_KEY = "appearance/theme"


def _build_palette(
    *,
    window: QColor,
    base: QColor,
    text: QColor,
    disabled_text: QColor,
    highlight: QColor,
    highlight_text: QColor = QColor(255, 255, 255),
    alt_base: QColor | None = None,
    button: QColor | None = None,
    link: QColor | None = None,
) -> QPalette:
    """Build a QPalette from the given color scheme."""
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, alt_base or base.lighter(110))
    p.setColor(QPalette.ColorRole.ToolTipBase, window.lighter(110))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, button or window)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.Link, link or highlight)
    p.setColor(QPalette.ColorRole.Highlight, highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, highlight_text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    return p


def _dark_palette() -> QPalette:
    return _build_palette(
        window=QColor(45, 45, 45),
        base=QColor(30, 30, 30),
        text=QColor(220, 220, 220),
        disabled_text=QColor(127, 127, 127),
        highlight=QColor(42, 130, 218),
        alt_base=QColor(53, 53, 53),
    )


def _warm_palette() -> QPalette:
    return _build_palette(
        window=QColor(60, 45, 35),
        base=QColor(45, 32, 25),
        text=QColor(235, 220, 200),
        disabled_text=QColor(140, 120, 100),
        highlight=QColor(180, 100, 40),
        link=QColor(220, 150, 60),
    )


def _cool_palette() -> QPalette:
    return _build_palette(
        window=QColor(35, 45, 60),
        base=QColor(25, 32, 45),
        text=QColor(200, 220, 240),
        disabled_text=QColor(100, 120, 140),
        highlight=QColor(60, 140, 200),
        link=QColor(100, 180, 240),
    )


def _high_contrast_palette() -> QPalette:
    return _build_palette(
        window=QColor(0, 0, 0),
        base=QColor(0, 0, 0),
        text=QColor(255, 255, 255),
        disabled_text=QColor(180, 180, 180),
        highlight=QColor(255, 255, 0),
        highlight_text=QColor(0, 0, 0),
        link=QColor(0, 255, 255),
    )


def _yellow_on_blue_palette() -> QPalette:
    return _build_palette(
        window=QColor(0, 0, 128),
        base=QColor(0, 0, 100),
        text=QColor(255, 255, 0),
        disabled_text=QColor(128, 128, 0),
        highlight=QColor(255, 255, 0),
        highlight_text=QColor(0, 0, 128),
        button=QColor(0, 0, 160),
        link=QColor(0, 255, 255),
    )


_PALETTE_BUILDERS: dict[ThemeMode, callable] = {
    ThemeMode.DARK: _dark_palette,
    ThemeMode.WARM: _warm_palette,
    ThemeMode.COOL: _cool_palette,
    ThemeMode.HIGH_CONTRAST: _high_contrast_palette,
    ThemeMode.YELLOW_ON_BLUE: _yellow_on_blue_palette,
}

_TOOLTIP_STYLES: dict[ThemeMode, str] = {
    ThemeMode.DARK: (
        "QToolTip { color: #dcdcdc; background-color: #323232; border: 1px solid #555; }"
    ),
    ThemeMode.WARM: (
        "QToolTip { color: #ebdcc8; background-color: #3c2d23; border: 1px solid #6b4c30; }"
    ),
    ThemeMode.COOL: (
        "QToolTip { color: #c8dcf0; background-color: #19203c; border: 1px solid #3c5a80; }"
    ),
    ThemeMode.HIGH_CONTRAST: (
        "QToolTip { color: #ffffff; background-color: #000000; border: 2px solid #ffffff; }"
    ),
    ThemeMode.YELLOW_ON_BLUE: (
        "QToolTip { color: #ffff00; background-color: #000064; border: 1px solid #ffff00; }"
    ),
}


class ThemeManager:
    """Manages application-wide theme.

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

        builder = _PALETTE_BUILDERS.get(mode)
        if builder is not None:
            app.setPalette(builder())
            app.setStyleSheet(_TOOLTIP_STYLES.get(mode, ""))
        else:
            # system or light — revert to platform defaults
            app.setPalette(self._system_palette)
            app.setStyleSheet("")

        settings = QSettings()
        settings.setValue(_SETTINGS_KEY, mode.value)

    @property
    def current(self) -> ThemeMode:
        return self._current
