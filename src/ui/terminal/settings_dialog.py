"""Terminal Settings dialog — font, theme, shell, environment variables.

Separate from the Claude session settings so terminal-specific preferences
(color theme, default shell) don't bleed into Claude session behaviour.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets.closing_combo_box import ClosingComboBox

# ---- Built-in terminal colour themes ----------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "Dark (Default)": {
        "fg": "#d0d0d0",
        "bg": "#1a1a1a",
        "black": "#000000",
        "red": "#cc0000",
        "green": "#4e9a06",
        "brown": "#c4a000",
        "blue": "#3465a4",
        "magenta": "#75507b",
        "cyan": "#06989a",
        "white": "#d3d7cf",
        "bright_black": "#555753",
        "bright_red": "#ef2929",
        "bright_green": "#8ae234",
        "bright_yellow": "#fce94f",
        "bright_blue": "#729fcf",
        "bright_magenta": "#ad7fa8",
        "bright_cyan": "#34e2e2",
        "bright_white": "#eeeeec",
    },
    "Solarized Dark": {
        "fg": "#839496",
        "bg": "#002b36",
        "black": "#073642",
        "red": "#dc322f",
        "green": "#859900",
        "brown": "#b58900",
        "blue": "#268bd2",
        "magenta": "#d33682",
        "cyan": "#2aa198",
        "white": "#eee8d5",
        "bright_black": "#586e75",
        "bright_red": "#cb4b16",
        "bright_green": "#859900",
        "bright_yellow": "#b58900",
        "bright_blue": "#268bd2",
        "bright_magenta": "#6c71c4",
        "bright_cyan": "#2aa198",
        "bright_white": "#fdf6e3",
    },
    "Solarized Light": {
        "fg": "#657b83",
        "bg": "#fdf6e3",
        "black": "#073642",
        "red": "#dc322f",
        "green": "#859900",
        "brown": "#b58900",
        "blue": "#268bd2",
        "magenta": "#d33682",
        "cyan": "#2aa198",
        "white": "#eee8d5",
        "bright_black": "#586e75",
        "bright_red": "#cb4b16",
        "bright_green": "#859900",
        "bright_yellow": "#b58900",
        "bright_blue": "#268bd2",
        "bright_magenta": "#6c71c4",
        "bright_cyan": "#2aa198",
        "bright_white": "#fdf6e3",
    },
    "Monokai": {
        "fg": "#f8f8f2",
        "bg": "#272822",
        "black": "#272822",
        "red": "#f92672",
        "green": "#a6e22e",
        "brown": "#fd971f",
        "blue": "#66d9ef",
        "magenta": "#ae81ff",
        "cyan": "#a1efe4",
        "white": "#f8f8f2",
        "bright_black": "#75715e",
        "bright_red": "#f92672",
        "bright_green": "#a6e22e",
        "bright_yellow": "#e6db74",
        "bright_blue": "#66d9ef",
        "bright_magenta": "#ae81ff",
        "bright_cyan": "#a1efe4",
        "bright_white": "#f9f8f5",
    },
    "Nord": {
        "fg": "#d8dee9",
        "bg": "#2e3440",
        "black": "#3b4252",
        "red": "#bf616a",
        "green": "#a3be8c",
        "brown": "#ebcb8b",
        "blue": "#81a1c1",
        "magenta": "#b48ead",
        "cyan": "#88c0d0",
        "white": "#e5e9f0",
        "bright_black": "#4c566a",
        "bright_red": "#bf616a",
        "bright_green": "#a3be8c",
        "bright_yellow": "#ebcb8b",
        "bright_blue": "#81a1c1",
        "bright_magenta": "#b48ead",
        "bright_cyan": "#8fbcbb",
        "bright_white": "#eceff4",
    },
}

# ---- QSettings keys ---------------------------------------------------------

_SETTINGS_GROUP = "terminal"
_KEY_THEME = "theme"
_KEY_FONT_FAMILY = "font_family"
_KEY_FONT_SIZE = "font_size"
_KEY_SHELL = "default_shell"
_KEY_ENV_VARS = "env_vars"


# ---- Helpers for reading persisted settings ---------------------------------


def load_terminal_settings() -> dict:
    """Read terminal settings from QSettings. Returns a dict with keys:
    theme, font_family, font_size, shell, env_vars.
    """
    s = QSettings("SharkDrown", "App")
    s.beginGroup(_SETTINGS_GROUP)
    result = {
        "theme": s.value(_KEY_THEME, "Dark (Default)"),
        "font_family": s.value(_KEY_FONT_FAMILY, ""),
        "font_size": s.value(_KEY_FONT_SIZE, 0, type=int),
        "shell": s.value(_KEY_SHELL, ""),
        "env_vars": s.value(_KEY_ENV_VARS, ""),
    }
    s.endGroup()
    return result


def save_terminal_settings(settings: dict) -> None:
    """Persist terminal settings to QSettings."""
    s = QSettings("SharkDrown", "App")
    s.beginGroup(_SETTINGS_GROUP)
    s.setValue(_KEY_THEME, settings.get("theme", "Dark (Default)"))
    s.setValue(_KEY_FONT_FAMILY, settings.get("font_family", ""))
    s.setValue(_KEY_FONT_SIZE, settings.get("font_size", 0))
    s.setValue(_KEY_SHELL, settings.get("shell", ""))
    s.setValue(_KEY_ENV_VARS, settings.get("env_vars", ""))
    s.endGroup()


def get_terminal_theme() -> dict[str, str]:
    """Return the active terminal colour theme dict."""
    settings = load_terminal_settings()
    name = settings["theme"]
    return THEMES.get(name, THEMES["Dark (Default)"])


# ---- Dialog -----------------------------------------------------------------


class TerminalSettingsDialog(QDialog):
    """Modal dialog for terminal-specific settings."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Terminal Settings")
        self.setModal(True)
        self.setMinimumWidth(480)

        self._current = load_terminal_settings()

        outer = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_appearance_tab(), "Appearance")
        tabs.addTab(self._build_shell_tab(), "Shell")
        outer.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ---- Tab builders --------------------------------------------------------

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        # Theme selector
        self._theme_combo = ClosingComboBox()
        self._theme_combo.addItems(list(THEMES.keys()))
        current_theme = self._current["theme"]
        idx = self._theme_combo.findText(current_theme)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        # ``activated`` instead of ``currentTextChanged`` so the popup
        # closes cleanly. ``_update_preview`` does heavy work
        # (stylesheet rebuild + preview re-render) that previously
        # left the popup stuck open. Programmatic ``setCurrentIndex``
        # above bypasses ``activated`` so the initial population
        # doesn't trigger a spurious preview update.
        self._theme_combo.activated.connect(
            lambda _i: self._update_preview(self._theme_combo.currentText())
        )
        form.addRow("Color theme:", self._theme_combo)

        # Font family
        self._font_combo = ClosingComboBox()
        self._font_combo.setEditable(True)
        mono_families = QFontDatabase.families(QFontDatabase.Latin)
        fixed_families = [
            f for f in mono_families if QFontDatabase.isFixedPitch(f)
        ]
        self._font_combo.addItem("(System Default)")
        self._font_combo.addItems(fixed_families)
        saved_family = self._current["font_family"]
        if saved_family:
            idx = self._font_combo.findText(saved_family)
            if idx >= 0:
                self._font_combo.setCurrentIndex(idx)
            else:
                self._font_combo.setEditText(saved_family)
        form.addRow("Font:", self._font_combo)

        # Font size
        self._size_spin = QSpinBox()
        self._size_spin.setRange(0, 32)
        self._size_spin.setSpecialValueText("(Default)")
        self._size_spin.setValue(self._current["font_size"])
        form.addRow("Font size (pt):", self._size_spin)

        # Preview
        self._preview_label = QLabel()
        self._preview_label.setFixedHeight(60)
        self._preview_label.setAlignment(Qt.AlignCenter)
        self._update_preview(self._theme_combo.currentText())
        form.addRow("Preview:", self._preview_label)

        return page

    def _build_shell_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        form = QFormLayout()

        # Default shell
        shell_row = QHBoxLayout()
        self._shell_edit = QLineEdit(self._current["shell"])
        self._shell_edit.setPlaceholderText(
            "(inherit $SHELL — e.g. /bin/zsh)"
        )
        shell_row.addWidget(self._shell_edit, 1)
        form.addRow("Default shell:", shell_row)

        layout.addLayout(form)

        # Environment variables
        env_label = QLabel(
            "Extra environment variables (KEY=VALUE, one per line).\n"
            "These are merged into the shell's environment for new sessions."
        )
        env_label.setWordWrap(True)
        layout.addWidget(env_label)

        self._env_edit = QPlainTextEdit()
        self._env_edit.setPlaceholderText(
            "EDITOR=nvim\nMANPAGER=less -R"
        )
        self._env_edit.setPlainText(self._current["env_vars"])
        layout.addWidget(self._env_edit, 1)

        return page

    # ---- Preview -------------------------------------------------------------

    def _update_preview(self, theme_name: str) -> None:
        theme = THEMES.get(theme_name, THEMES["Dark (Default)"])
        fg = theme["fg"]
        bg = theme["bg"]
        green = theme["green"]
        blue = theme["blue"]
        self._preview_label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; "
            f"font-family: monospace; padding: 8px; border-radius: 4px;"
        )
        self._preview_label.setText(
            f'<span style="color:{green}">user@host</span>'
            f'<span style="color:{fg}">:</span>'
            f'<span style="color:{blue}">~/projects</span>'
            f'<span style="color:{fg}">$ ls -la</span>'
        )

    # ---- Accept --------------------------------------------------------------

    def _on_accept(self) -> None:
        font_family = self._font_combo.currentText()
        if font_family == "(System Default)":
            font_family = ""

        settings = {
            "theme": self._theme_combo.currentText(),
            "font_family": font_family,
            "font_size": self._size_spin.value(),
            "shell": self._shell_edit.text().strip(),
            "env_vars": self._env_edit.toPlainText(),
        }
        save_terminal_settings(settings)
        self.accept()

    # ---- Public API ----------------------------------------------------------

    @staticmethod
    def open_dialog(parent=None) -> bool:
        """Show the dialog modally. Returns True if settings were changed."""
        dlg = TerminalSettingsDialog(parent)
        return dlg.exec() == QDialog.Accepted
