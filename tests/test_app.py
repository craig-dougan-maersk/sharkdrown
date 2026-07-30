"""Simple test to verify the application starts correctly."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from src.ui.main_window import MainWindow


def test_app(tmp_path):
    """Test that the main window can be created."""
    # Establish org/app identity BEFORE isolating QSettings so that
    # QSettings().clear() targets the correct settings file.  If we set
    # the path first, clear() runs under ("", "") and the real user plist
    # (which may have autostart=True) remains uncleared; MainWindow then
    # reads it and fires QTimer.singleShot(0, _start_server), hanging forever.
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setOrganizationName("Sharkdrown")
    app.setApplicationName("PandasMarkdownEditor")

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    QSettings().clear()

    window = MainWindow()
    window.show()

    # Verify window exists
    assert window is not None
    # The window title comes from ``src.config.APP_TITLE`` — assert the
    # constant rather than the literal so a rename in one place flows
    # through here automatically.
    from src.config import APP_TITLE
    assert window.windowTitle() == APP_TITLE

    print("✓ Main window created successfully")
    print(f"✓ Window size: {window.size().width()}x{window.size().height()}")

    # Check editor tabs
    from src.ui.widgets.editor_widget import EditorWidget
    editor = window.editor_tabs.widget(0)
    assert isinstance(editor, EditorWidget)
    print("✓ Editor widget created successfully")

    # With the new defaults (split_enabled=False, preview_visible=False),
    # no preview tab is created on the initial New. Verify the empty
    # state, then toggle Preview on and confirm a preview tab materialises.
    assert window.preview_tabs.count() == 0, (
        "no preview tab should be created when split + preview are off"
    )
    print("✓ Preview tab is empty by default (single-pane editor)")

    window.action_toggle_preview.trigger()
    assert window.preview_visible is True
    assert window.split_enabled is True
    from src.ui.widgets.preview_widget import PreviewWidget
    preview = window.preview_tabs.widget(0)
    assert isinstance(preview, PreviewWidget)
    print("✓ Toggling Preview creates a preview tab and enables split")

    # Test document
    from src.core.document import DocumentTab
    doc = DocumentTab()
    assert doc.format_type == "markdown"
    assert doc.title() == "Untitled"
    print("✓ Document model works correctly")

    # Regression: clicking File → New used to break with
    # ``setPlainText(bool)`` because ``triggered`` emits a bool that was
    # being passed as ``content`` to ``_create_new_tab``. (The toolbar
    # ``tb_new`` action it used to call was removed along with the
    # toolbar — File → New is the only entry point now.)
    #
    # NewFileDialog.ask() calls dlg.exec() which blocks waiting for user
    # input — in headless test runs there is no one to click OK. Patch
    # the static method so it returns "markdown" immediately without
    # showing a modal dialog.
    from unittest.mock import patch

    initial_tabs = window.editor_tabs.count()
    with patch(
        "src.ui.dialogs.new_file_dialog.NewFileDialog.ask",
        return_value="markdown",
    ):
        window.action_new.trigger()
        window.action_new.trigger()
        window.action_new.trigger()
    assert window.editor_tabs.count() == initial_tabs + 3, (
        f"expected {initial_tabs + 3} tabs after 3 New clicks, "
        f"got {window.editor_tabs.count()}"
    )
    print("✓ New File action works on multiple clicks (regression)")

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    test_app()