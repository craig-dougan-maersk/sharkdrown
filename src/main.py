"""
Main entry point for the Markdown Editor application.

This module initializes the Qt application and launches the main window.
"""

import os
import sys
from pathlib import Path
from src.config import APP_TITLE, APP_NAME, APP_ORG

# Configure QtWebEngine BEFORE importing any PySide6 widgets. Chromium
# reads these flags at process start to decide whether to spin up a GPU
# subprocess. On WSL2 / headless containers without GPU passthrough the
# Mesa/EGL stack fails to initialise, producing a wall of libEGL errors
# and a degraded rendering experience. Disabling GPU acceleration up
# front routes Chromium through software rendering and silences those
# warnings. Users with a working GPU can override via env var.
if "QTWEBENGINE_CHROMIUM_FLAGS" not in os.environ:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu"

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.ui.main_window import MainWindow  # noqa: E402


def main() -> None:
    """Create and run the main application."""
    app = QApplication(sys.argv)
    # Organization + application name MUST be set before constructing
    # MainWindow so that QStandardPaths.writableLocation(AppDataLocation)
    # and QSettings() resolve to per-app, platform-appropriate paths:
    #   Linux:   ~/.local/share/Sharkdrown/PandasMarkdownEditor/
    #   macOS:   ~/Library/Application Support/Sharkdrown/PandasMarkdownEditor/
    #   Windows: %APPDATA%/Sharkdrown/PandasMarkdownEditor/
    app.setOrganizationName(APP_ORG)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    app.setApplicationVersion("0.1.0")

    # Set the application icon if available
    icon_path = Path(__file__).parent / "resources" / "icons" / "app_icon.png"
    if icon_path.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    # Restore persisted UI font preferences.
    from PySide6.QtCore import QSettings
    settings = QSettings("SharkDrown", "App")
    # Default font size bumped from system-derived (typically 10-12pt) to
    # 14 in the multi-bug-fix milestone (issue #2). The user explicitly
    # chose to force-override existing users too — any persisted base
    # size < 14 is rewritten to 14 so first-launch AND upgrade users
    # land at the new default. Users who have explicitly set a size ≥ 14
    # are left alone; the migration only catches the previous default.
    DEFAULT_BASE_FONT_SIZE = 14
    saved_base = settings.value("app_font_base_size", None, type=int)
    if saved_base is None or saved_base < DEFAULT_BASE_FONT_SIZE:
        settings.setValue("app_font_base_size", DEFAULT_BASE_FONT_SIZE)
    font = app.font()
    # Restore font family
    saved_family = settings.value("ui_font_family", "System Default")
    if saved_family != "System Default":
        font.setFamily(saved_family)
    # Resolve the effective point size: persisted base + user delta,
    # clamped to the same [8, 32] range the rest of the UI uses. Done
    # unconditionally so users with no family change AND no delta still
    # land at the new default base size when their persisted value was
    # bumped above.
    font_delta = settings.value("app_font_delta", 0, type=int)
    base = settings.value("app_font_base_size", font.pointSize(), type=int)
    font.setPointSize(max(8, min(32, base + font_delta)))
    if (
        saved_family != "System Default"
        or font_delta
        or font.pointSize() != app.font().pointSize()
    ):
        app.setFont(font)

    # Create and show the main window
    window = MainWindow()
    window.show()

    # Run the event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
