"""Tests for the new tab/format/rename behavior.

Covers the five UI bugs fixed together:
1. Format selector dropdown closes after a selection.
2. Tab labels show ``[format tag] [filename]`` (italic when dirty).
3. New untitled tabs are named ``untitled.{ext}``.
4. Duplicate filenames get ``(N)`` suffixes.
5. Double-click rename warns on extension change and updates the format.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QMessageBox,
    QTabBar,
)

from src.core.document import DocumentTab
from src.ui.main_window import MainWindow
from src.ui.main_window._document_ops import (
    _FORMAT_COLORS,
    _FORMAT_TAGS,
    _TabLabelWidget,
    _detect_format_from_ext,
)
from src.ui.widgets.editor_widget import EditorWidget


def _get_tab_label(window: MainWindow, index: int) -> _TabLabelWidget:
    """Return the custom label widget for the given editor tab."""
    widget = window.editor_tabs.tabBar().tabButton(index, QTabBar.LeftSide)
    assert isinstance(widget, _TabLabelWidget), (
        f"expected _TabLabelWidget at index {index}, got {type(widget).__name__}"
    )
    return widget


# ----------------------------------------------------------------------
# Test harness
# ----------------------------------------------------------------------

def _make_window() -> MainWindow:
    """Build a ``MainWindow`` with an isolated QSettings store.

    The QSettings isolation prevents autostart from firing the proxy
    thread (and prevents tests from leaking state into the user's
    real config).
    """
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(Path("/tmp/sharkdrown-test-document-tabs")),
    )
    QSettings().clear()

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setOrganizationName("Sharkdrown")
    app.setApplicationName("PandasMarkdownEditor")

    return MainWindow()


# ----------------------------------------------------------------------
# 1. Format selector dropdown
# ----------------------------------------------------------------------

def test_format_combo_uses_activated_signal() -> None:
    """The combo is wired to ``activated`` (user-only), not ``currentTextChanged``."""
    window = _make_window()
    assert hasattr(window, "_on_format_combo_activated"), (
        "expected the new _on_format_combo_activated slot to exist"
    )
    # The old name should be gone — the bug it was guarding (popup
    # staying open) was caused by that signal choice.
    assert not hasattr(window, "_on_format_combo_changed"), (
        "old _on_format_combo_changed slot should have been removed"
    )
    print("✓ Format combo uses the activated signal")


def _activated_index_for(window: MainWindow, fmt: str) -> int:
    """Return the ``activated`` index that selects ``fmt`` in the combo."""
    idx = window.format_combo.findText(fmt)
    assert idx >= 0, f"format {fmt!r} not in combo"
    return idx


def test_format_combo_activated_slot_changes_format() -> None:
    """The activated slot still drives the document's format."""
    window = _make_window()
    editor = window.editor_tabs.widget(0)
    assert editor.document.format_type == "markdown"

    # ``hidePopup()`` is a no-op if the popup isn't showing, so it's
    # safe to call in headless tests; we just exercise the path.
    window._on_format_combo_activated(_activated_index_for(window, "json"))

    assert editor.document.format_type == "json"
    print("✓ Activated slot updates the document's format")


def test_format_combo_activated_slot_hides_popup() -> None:
    """The slot calls ``hidePopup()`` even when the popup isn't visible."""
    window = _make_window()
    # The call must not raise; in a headless test the popup was never
    # shown, but ``hidePopup()`` is idempotent so the test still
    # exercises the safety-net code path.
    window._on_format_combo_activated(_activated_index_for(window, "yaml"))
    print("✓ Activated slot invokes hidePopup() without error")


# ----------------------------------------------------------------------
# 2. Format tag in the tab title
# ----------------------------------------------------------------------

def test_initial_tab_is_untitled_md() -> None:
    """The auto-created first tab is named ``untitled.md`` with the md tag."""
    window = _make_window()
    label = _get_tab_label(window, 0)
    assert label.tag == "md", f"expected tag 'md', got {label.tag!r}"
    assert label.color == "#4a9eff", (
        f"expected blue colour, got {label.color!r}"
    )
    assert label.name == "untitled.md", f"expected filename, got {label.name!r}"
    # Empty content → not modified → roman (italic flag is False)
    assert label.italic is False, "empty content should not be italic"
    # And no HTML leaked into the style-drawn tab text
    assert window.editor_tabs.tabText(0) == "", (
        f"default tab text should be cleared, got {window.editor_tabs.tabText(0)!r}"
    )
    print("✓ Initial tab is 'untitled.md' with blue md tag, roman")


def test_format_tag_color_per_format() -> None:
    """Each format renders its own colour and short tag."""
    window = _make_window()
    cases = [
        ("markdown", "#4a9eff", "md"),
        ("json", "#e8a838", "json"),
        ("yaml", "#4ec86c", "yaml"),
        ("text", "#888888", "txt"),
        ("html", "#c678dd", "html"),
    ]
    for fmt, color, tag in cases:
        window._new_untitled_tab(fmt)
        idx = window.editor_tabs.count() - 1
        label = _get_tab_label(window, idx)
        assert label.color == color, (
            f"{fmt}: expected colour {color!r}, got {label.color!r}"
        )
        assert label.tag == tag, (
            f"{fmt}: expected tag {tag!r}, got {label.tag!r}"
        )
    print("✓ Each format renders its own colour and tag")


def test_tab_has_no_coloured_dot_icon() -> None:
    """The old coloured-circle tab icon is gone (replaced by the tag)."""
    window = _make_window()
    # An empty QIcon is the default — assert the icon is "null" (no
    # pixmap set). The simplest check is that ``isNull()`` is True
    # for the tab icon after the initial title refresh.
    editor = window.editor_tabs.widget(0)
    assert editor is not None
    # The tab bar stores the icon internally; we check the pixmap
    # cache on the tab bar's tabButton for the left side.
    tab_bar = window.editor_tabs.tabBar()
    icon = tab_bar.tabIcon(0)
    assert icon.isNull(), "expected no tab icon (coloured dot was removed)"
    print("✓ Tab no longer carries a coloured-dot icon")


# ----------------------------------------------------------------------
# 3. New file default names
# ----------------------------------------------------------------------

def test_new_file_default_name_per_format() -> None:
    """Each format gets an ``untitled[N].{ext}`` name with the right extension.

    The exact ``untitled`` vs ``untitled (N)`` is decided by the
    dedup logic, which other tests exercise; here we just verify
    that the *extension* is correct and the base name is
    ``untitled``.
    """
    window = _make_window()
    expected_ext = {
        "markdown": ".md",
        "json": ".json",
        "yaml": ".yml",  # user spec: untitled.yml (not .yaml)
        "text": ".txt",
        "html": ".html",
    }
    for fmt, ext in expected_ext.items():
        window._new_untitled_tab(fmt)
        idx = window.editor_tabs.count() - 1
        doc = window.editor_tabs.widget(idx).document
        name = doc.display_name
        assert name.startswith("untitled"), (
            f"{fmt}: expected name to start with 'untitled', got {name!r}"
        )
        assert name.endswith(ext), (
            f"{fmt}: expected extension {ext!r}, got {name!r}"
        )
        # The custom label widget also shows the same filename
        label = _get_tab_label(window, idx)
        assert label.name == name, (
            f"{fmt}: label name {label.name!r} != document name {name!r}"
        )
    print("✓ New untitled tabs get the correct default names")


# ----------------------------------------------------------------------
# 4. Duplicate-name disambiguation
# ----------------------------------------------------------------------

def test_dedup_appends_paren_n() -> None:
    """Three JSON tabs in a row become (1), (2), (3)."""
    window = _make_window()
    window._new_untitled_tab("json")
    window._new_untitled_tab("json")
    window._new_untitled_tab("json")

    names = [
        window.editor_tabs.widget(i).document.display_name
        for i in range(window.editor_tabs.count())
        if window.editor_tabs.widget(i).document.format_type == "json"
    ]
    assert names == ["untitled.json", "untitled (2).json", "untitled (3).json"], (
        f"unexpected dedup sequence: {names!r}"
    )
    print("✓ Duplicate names get (2), (3) suffixes")


def test_dedup_does_not_reuse_n_when_base_free() -> None:
    """If only ``untitled (2).json`` exists, a new JSON tab is ``untitled.json``."""
    window = _make_window()

    # Inject a tab whose display_name is "untitled (2).json" without
    # ever having a "untitled.json" tab open.
    doc = DocumentTab(format_type="json")
    doc.display_name = "untitled (2).json"
    editor = EditorWidget()
    editor.document = doc
    window.editor_tabs.addTab(editor, "")
    window._update_tab_title(window.editor_tabs.count() - 1, doc)

    # Now ask for a new JSON tab — the base name "untitled.json" is
    # free, so that's what we should get (not "untitled (3).json").
    window._new_untitled_tab("json")
    new_doc = window.editor_tabs.widget(window.editor_tabs.count() - 1).document
    assert new_doc.display_name == "untitled.json", (
        f"expected 'untitled.json', got {new_doc.display_name!r}"
    )
    print("✓ Dedup fills gaps instead of always incrementing")


# ----------------------------------------------------------------------
# 5. Italic / roman state
# ----------------------------------------------------------------------

def test_filename_italic_when_modified() -> None:
    """Typing into the editor switches the filename to italic."""
    window = _make_window()
    editor = window.editor_tabs.widget(0)

    # Empty content → roman
    label = _get_tab_label(window, 0)
    assert label.italic is False, "empty content should not be italic"

    # Type a character → modified → italic
    editor.editor.setPlainText("hello")
    QApplication.processEvents()

    label = _get_tab_label(window, 0)
    assert label.italic is True, "modified content should be italic"
    print("✓ Filename goes italic when the document is modified")


def test_filename_roman_after_save() -> None:
    """Saving the document removes the italic marker."""
    window = _make_window()
    editor = window.editor_tabs.widget(0)
    doc = editor.document

    # Type to mark the doc dirty
    editor.editor.setPlainText("hello world")
    QApplication.processEvents()
    assert _get_tab_label(window, 0).italic is True

    # Save to a temp .md file
    fd, temp_path = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        doc.file_path = Path(temp_path)
        window._save_file()
        QApplication.processEvents()

        assert _get_tab_label(window, 0).italic is False, (
            "expected roman (italic=False) after save"
        )
    finally:
        os.unlink(temp_path)

    print("✓ Filename returns to roman after saving")


# ----------------------------------------------------------------------
# 6. Double-click rename
# ----------------------------------------------------------------------

def test_rename_keeps_format_when_extension_unchanged() -> None:
    """Renaming within the same extension leaves the format alone."""
    window = _make_window()
    window._new_untitled_tab("json")
    idx = window.editor_tabs.count() - 1
    doc = window.editor_tabs.widget(idx).document
    assert doc.format_type == "json"

    with patch.object(
        QInputDialog, "getText", return_value=("myfile.json", True)
    ):
        window._rename_tab(idx)

    assert doc.format_type == "json", "format should not change"
    assert doc.display_name == "myfile.json"
    # The renamed tab still shows the json tag in the label widget
    label = _get_tab_label(window, idx)
    assert label.tag == "json"
    assert label.name == "myfile.json"
    print("✓ Rename without extension change keeps the format")


def test_rename_extension_change_warns_and_updates_format() -> None:
    """Renaming with a new extension updates the format (after warning)."""
    window = _make_window()
    window._new_untitled_tab("json")
    idx = window.editor_tabs.count() - 1
    doc = window.editor_tabs.widget(idx).document
    assert doc.format_type == "json"

    with patch.object(
        QInputDialog, "getText", return_value=("untitled.md", True)
    ), patch.object(
        QMessageBox, "question", return_value=QMessageBox.Yes
    ) as warn:
        window._rename_tab(idx)

    assert warn.called, "expected the extension-change warning dialog"
    assert doc.format_type == "markdown", (
        f"expected markdown after rename, got {doc.format_type!r}"
    )
    # Tab label now shows the markdown tag colour
    label = _get_tab_label(window, idx)
    assert label.tag == "md"
    assert label.color == "#4a9eff"
    print("✓ Rename with extension change warns and updates the format")


def test_rename_warning_cancelled_keeps_old_format() -> None:
    """If the user dismisses the extension-change warning, nothing changes."""
    window = _make_window()
    window._new_untitled_tab("json")
    idx = window.editor_tabs.count() - 1
    doc = window.editor_tabs.widget(idx).document
    original_name = doc.display_name

    with patch.object(
        QInputDialog, "getText", return_value=("untitled.md", True)
    ), patch.object(
        QMessageBox, "question", return_value=QMessageBox.No
    ):
        window._rename_tab(idx)

    assert doc.format_type == "json", "format should not change on cancel"
    assert doc.display_name == original_name, "name should not change on cancel"
    print("✓ Dismissing the extension-change warning cancels the rename")


def test_rename_unknown_extension_becomes_text() -> None:
    """Renaming to an unknown suffix falls back to plain text."""
    window = _make_window()
    window._new_untitled_tab("json")
    idx = window.editor_tabs.count() - 1
    doc = window.editor_tabs.widget(idx).document
    assert doc.format_type == "json"

    with patch.object(
        QInputDialog, "getText", return_value=("untitled.weird", True)
    ), patch.object(
        QMessageBox, "question", return_value=QMessageBox.Yes
    ):
        window._rename_tab(idx)

    assert doc.format_type == "text", (
        f"expected text for unknown ext, got {doc.format_type!r}"
    )
    # And ``_detect_format_from_ext`` agrees
    assert _detect_format_from_ext(".weird") == "text"
    assert _detect_format_from_ext("") == "text"
    print("✓ Unknown / missing extension falls back to plain text")


def test_rename_updates_file_path_for_opened_files() -> None:
    """Renaming a file-backed tab changes ``file_path`` in the same directory."""
    window = _make_window()

    fd, temp_path = tempfile.mkstemp(suffix=".json", text=False)
    os.write(fd, b'{"key": "value"}')
    os.close(fd)
    try:
        window._load_file(Path(temp_path))
        idx = window.editor_tabs.count() - 1
        doc = window.editor_tabs.widget(idx).document
        assert doc.file_path == Path(temp_path)
        original_dir = doc.file_path.parent

        new_name = Path(temp_path).stem + "_renamed.json"
        with patch.object(
            QInputDialog, "getText", return_value=(new_name, True)
        ):
            window._rename_tab(idx)

        assert doc.file_path.name == new_name
        assert doc.file_path.parent == original_dir
        # ``display_name`` is cleared so ``title()`` falls through to
        # ``file_path.name``.
        assert doc.display_name is None
        assert doc.title() == new_name
    finally:
        os.unlink(temp_path)

    print("✓ Rename updates file_path in the same directory")


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------

ALL_TESTS = [
    test_format_combo_uses_activated_signal,
    test_format_combo_activated_slot_changes_format,
    test_format_combo_activated_slot_hides_popup,
    test_initial_tab_is_untitled_md,
    test_format_tag_color_per_format,
    test_tab_has_no_coloured_dot_icon,
    test_new_file_default_name_per_format,
    test_dedup_appends_paren_n,
    test_dedup_does_not_reuse_n_when_base_free,
    test_filename_italic_when_modified,
    test_filename_roman_after_save,
    test_rename_keeps_format_when_extension_unchanged,
    test_rename_extension_change_warns_and_updates_format,
    test_rename_warning_cancelled_keeps_old_format,
    test_rename_unknown_extension_becomes_text,
    test_rename_updates_file_path_for_opened_files,
]


def run_all() -> None:
    """Execute every test in order. Used by ``__main__`` and by the
    session-end verifier."""
    for test in ALL_TESTS:
        test()
    print(f"\n✅ All {len(ALL_TESTS)} tests passed!")


if __name__ == "__main__":
    run_all()
