"""Simple test to verify the application starts correctly."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from src.ui.main_window import MainWindow


def test_app():
    """Test that the main window can be created."""
    # Use an isolated QSettings store so the test never reads the user's
    # real config (autostart would otherwise fire the proxy thread).
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(Path("/tmp/sharkdrown-test-app")),
    )
    QSettings().clear()

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setOrganizationName("Sharkdrown")
    app.setApplicationName("PandasMarkdownEditor")

    window = MainWindow()
    window.show()

    # Verify window exists
    assert window is not None
    assert window.windowTitle() == "Pandas Markdown Editor"

    print("✓ Main window created successfully")
    print(f"✓ Window size: {window.size().width()}x{window.size().height()}")

    # Check editor tabs
    from src.ui.widgets.editor_widget import EditorWidget
    editor = window.editor_tabs.widget(0)
    assert isinstance(editor, EditorWidget)
    print("✓ Editor widget created successfully")

    # Check preview tabs
    from src.ui.widgets.preview_widget import PreviewWidget
    preview = window.preview_tabs.widget(0)
    assert isinstance(preview, PreviewWidget)
    print("✓ Preview widget created successfully")

    # Test document
    from src.core.document import DocumentTab
    doc = DocumentTab()
    assert doc.format_type == "markdown"
    assert doc.title() == "Untitled"
    print("✓ Document model works correctly")

    # Regression: clicking File → New used to break with
    # ``setPlainText(bool)`` because ``triggered`` emits a bool that was
    # being passed as ``content`` to ``_create_new_tab``.
    initial_tabs = window.editor_tabs.count()
    window.action_new.trigger()
    window.action_new.trigger()
    window.tb_new.trigger()
    assert window.editor_tabs.count() == initial_tabs + 3, (
        f"expected {initial_tabs + 3} tabs after 3 New clicks, "
        f"got {window.editor_tabs.count()}"
    )
    print("✓ New File action works on multiple clicks (regression)")

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    test_app()