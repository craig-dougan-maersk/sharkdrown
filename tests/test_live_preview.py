"""Regression test for the live-preview feature (Pass 1 / C1).

The previous build connected text_changed -> tab-title refresh, but never
wrote the editor's text back to DocumentTab.content. The preview always
showed stale content. This test proves the write-back chain works.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication

from src.core.document import DocumentTab
from src.ui.widgets.editor_widget import EditorWidget
from src.ui.widgets.preview_widget import PreviewWidget


def _app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    return app


def test_editor_writes_back_to_document() -> None:
    """Editing the editor should update DocumentTab.content."""
    _app()
    doc = DocumentTab()
    doc.content = "hello"
    editor = EditorWidget()
    editor.document = doc

    editor.setPlainText("hello world")
    assert doc.content == "hello world", f"got {doc.content!r}"
    print("✓ editor write-back updates DocumentTab.content")


def test_content_changed_signal_carries_text() -> None:
    """content_changed should emit the new plain text on every edit."""
    _app()
    doc = DocumentTab()
    editor = EditorWidget()
    editor.document = doc

    seen: list[str] = []
    editor.content_changed.connect(lambda t: seen.append(t))

    editor.setPlainText("abc")
    editor.setPlainText("abcdef")

    assert seen == ["abc", "abcdef"], f"got {seen!r}"
    print("✓ content_changed signal carries text")


def test_live_preview_re_renders_on_edit() -> None:
    """After an editor edit, the paired preview reflects the new content."""
    _app()
    doc = DocumentTab(content="# Title\n\n```pandas\nimport pandas as pd\ndf = pd.DataFrame({'A': [1, 2]})\ndf\n```\n")
    editor = EditorWidget()
    editor.document = doc

    preview = PreviewWidget()
    preview.set_document(doc)

    # Baseline: preview contains 'A' but not 'XYZ'.
    preview.preview.document().setHtml("<p>placeholder</p>")  # force a different state
    preview.set_document(doc)
    initial_text = preview.preview.toPlainText()
    assert "A" in initial_text
    assert "XYZ" not in initial_text

    # User edits the document — replace the table with new content.
    editor.setPlainText(doc.content + "\n\nAfter XYZ\n")
    # Editor already wrote back to doc.content; now main_window.py would
    # forward by calling preview._render_preview(). Replicate that here.
    preview._render_preview()

    after_text = preview.preview.toPlainText()
    assert "XYZ" in after_text, f"preview did not update; got: {after_text!r}"
    print("✓ preview re-renders after editor edit (live preview works)")


if __name__ == "__main__":
    test_editor_writes_back_to_document()
    test_content_changed_signal_carries_text()
    test_live_preview_re_renders_on_edit()
    print("\n✅ Live-preview regression suite passed.")
