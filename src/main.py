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

    # Create and show the main window
    window = MainWindow()
    window.show()

    # Run the event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
