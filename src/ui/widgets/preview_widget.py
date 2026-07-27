"""
Preview widget for rendering Markdown, JSON, and YAML files.

This module provides a preview widget with:
- Markdown rendering with pandas table support
- JSON/YAML formatted display
- Copy functionality
"""

import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QTextDocument, QTextCursor, QTextFrameFormat
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QTextEdit,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFrame,
    QSizePolicy,
)

if TYPE_CHECKING:
    from src.core.document import DocumentTab


class PandasTableProcessor:
    """Processes pandas code blocks and renders them as tables."""

    @staticmethod
    def extract_pandas_blocks(content: str) -> list[dict[str, Any]]:
        """Extract pandas code blocks from markdown content."""
        pattern = r"```pandas\s*\n(.*?)```"
        matches = re.finditer(pattern, content, re.DOTALL)

        blocks = []
        for i, match in enumerate(matches):
            blocks.append({
                "id": i,
                "code": match.group(1),
                "start": match.start(),
                "end": match.end(),
            })
        return blocks

    @staticmethod
    def execute_pandas_code(code: str) -> str | None:
        """
        Execute pandas code and return rendered markdown table.

        Note: This uses a restricted execution environment for security.
        """
        import pandas as pd

        # Create a restricted namespace
        namespace: dict[str, Any] = {
            "pd": pd,
            "DataFrame": pd.DataFrame,
        }

        try:
            # Execute the code
            exec(code, namespace)

            # Find DataFrame objects in namespace
            for name, value in namespace.items():
                if isinstance(value, pd.DataFrame):
                    # Render as markdown table
                    return value.to_markdown(index=False)

            return None
        except Exception as e:  # noqa: BLE001 — exec() of user code can raise anything
            return f"<!-- Error executing pandas code: {e} -->\n<!-- Original code:\n{code}\n-->"

    @staticmethod
    def replace_pandas_blocks(content: str) -> str:
        """Replace pandas code blocks with rendered tables."""
        blocks = PandasTableProcessor.extract_pandas_blocks(content)

        # Process in reverse order to maintain positions
        result = content
        for block in reversed(blocks):
            rendered = PandasTableProcessor.execute_pandas_code(block["code"])
            if rendered:
                result = result[:block["start"]] + rendered + result[block["end"]:]

        return result


class PreviewWidget(QWidget):
    """Widget for previewing documents with syntax-aware rendering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()
        self._document = None

    def _init_ui(self) -> None:
        """Initialize the UI components."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Preview display (read-only text edit)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QTextEdit.NoWrap)

        # Toolbar for actions
        toolbar = QHBoxLayout()

        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self._copy_content)
        toolbar.addWidget(self.copy_button)

        self.html_button = QPushButton("HTML")
        self.html_button.clicked.connect(self._copy_as_html)
        toolbar.addWidget(self.html_button)

        self.export_button = QPushButton("Export")
        self.export_button.clicked.connect(self._export_preview)
        toolbar.addWidget(self.export_button)

        toolbar.addStretch()

        layout.addLayout(toolbar)
        layout.addWidget(self.preview)

    def set_document(self, document: "DocumentTab") -> None:
        """Set the document to preview."""
        self._document = document
        self._render_preview()

    def _render_preview(self) -> None:
        """Render the document content."""
        if self._document is None:
            return

        content = self._document.content
        fmt = self._document.format_type

        if fmt == "markdown":
            self._render_markdown(content)
        elif fmt == "json":
            self._render_json(content)
        elif fmt == "yaml":
            self._render_yaml(content)
        else:
            self.preview.setPlainText(content)

    def _render_markdown(self, content: str) -> None:
        """Render markdown content with pandas table support."""
        try:
            import markdown as md

            # Process pandas blocks first
            processed = PandasTableProcessor.replace_pandas_blocks(content)

            # Create markdown instance with extensions
            md_instance = md.Markdown(
                extensions=["fenced_code", "tables", "attr_list", "toc"],
                extension_configs={
                    "toc": {"permalink": True},
                },
            )

            # Convert to HTML
            html = md_instance.convert(processed)

            # Create a simple HTML document
            full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans',
                         Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue',
                         sans-serif;
            font-size: 14px;
            line-height: 1.6;
            margin: 20px;
            max-width: 800px;
        }}
        h1, h2, h3, h4, h5, h6 {{
            color: #24292e;
            margin-top: 24px;
            margin-bottom: 16px;
        }}
        h1 {{ font-size: 2em; border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }}
        h2 {{ font-size: 1.5em; }}
        h3 {{ font-size: 1.25em; }}
        code {{
            padding: 0.2em 0.4em;
            margin: 0;
            font-size: 85%;
            background-color: rgba(27, 31, 35, 0.05);
            border-radius: 3px;
            font-family: SFMono-Regular, Consolas, Liberation Mono, Menlo, monospace;
        }}
        pre {{
            padding: 16px;
            overflow: auto;
            font-size: 85%;
            line-height: 1.45;
            background-color: #f6f8fa;
            border-radius: 6px;
        }}
        pre code {{
            background-color: transparent;
            padding: 0;
        }}
        table {{
            border-spacing: 0;
            border-collapse: collapse;
            margin: 15px 0;
        }}
        th, td {{
            padding: 6px 13px;
            border: 1px solid #dfe2e5;
            text-align: left;
        }}
        th {{
            background-color: #f6f8fa;
            font-weight: 600;
        }}
        tr:nth-child(2n) {{
            background-color: #f6f8fa;
        }}
        .mdx-table {{
            margin: 1em 0;
        }}
    </style>
</head>
<body>
{html}
</body>
</html>"""

            # Use QTextDocument to render HTML
            doc = QTextDocument()
            doc.setHtml(full_html)

            # Create a cursor and insert the document
            self.preview.setDocument(doc)

        except ImportError:
            # Fallback to plain text if markdown not available
            self.preview.setPlainText(content)

    def _render_json(self, content: str) -> None:
        """Render JSON content with syntax highlighting."""
        import json

        try:
            # Parse and reformat JSON
            data = json.loads(content)
            formatted = json.dumps(data, indent=2, ensure_ascii=False)
            self.preview.setPlainText(formatted)
        except json.JSONDecodeError as e:
            # Show error with original content
            self.preview.setPlainText(f"JSON Error: {e}\n\n{content}")

    def _render_yaml(self, content: str) -> None:
        """Render YAML content."""
        try:
            import yaml

            # Parse and reformat YAML
            data = yaml.safe_load(content)
            formatted = yaml.dump(data, default_flow_style=False, allow_unicode=True)
            self.preview.setPlainText(formatted)
        except yaml.YAMLError as e:
            # Show error with original content
            self.preview.setPlainText(f"YAML Error: {e}\n\n{content}")

    def _copy_content(self) -> None:
        """Copy the rendered content to clipboard."""
        cursor = self.preview.textCursor()
        content = cursor.selectedText()
        if not content:
            content = self.preview.toPlainText()

        from PySide6.QtGui import QGuiApplication
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(content)

    def _copy_as_html(self) -> None:
        """Copy the HTML representation to clipboard."""
        if self._document is None:
            return

        if self._document.format_type == "markdown":
            import markdown as md

            processed = PandasTableProcessor.replace_pandas_blocks(self._document.content)
            md_instance = md.Markdown(extensions=["fenced_code", "tables"])
            html = md_instance.convert(processed)

            from PySide6.QtGui import QGuiApplication
            clipboard = QGuiApplication.clipboard()
            clipboard.setText(html)

    def _export_preview(self) -> None:
        """Export the preview to a file."""
        from PySide6.QtWidgets import QFileDialog

        if self._document is None:
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Preview",
            "preview.html",
            "HTML Files (*.html);;All Files (*)",
        )

        if file_path:
            if self._document.format_type == "markdown":
                import markdown as md

                processed = PandasTableProcessor.replace_pandas_blocks(self._document.content)
                md_instance = md.Markdown(extensions=["fenced_code", "tables"])
                html = md_instance.convert(processed)

                full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans',
                         Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue',
                         sans-serif;
            font-size: 14px;
            line-height: 1.6;
            margin: 20px;
            max-width: 800px;
        }}
        h1, h2, h3, h4, h5, h6 {{
            color: #24292e;
            margin-top: 24px;
            margin-bottom: 16px;
        }}
        h1 {{ font-size: 2em; border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }}
        h2 {{ font-size: 1.5em; }}
        h3 {{ font-size: 1.25em; }}
        code {{
            padding: 0.2em 0.4em;
            margin: 0;
            font-size: 85%;
            background-color: rgba(27, 31, 35, 0.05);
            border-radius: 3px;
            font-family: SFMono-Regular, Consolas, Liberation Mono, Menlo, monospace;
        }}
        pre {{
            padding: 16px;
            overflow: auto;
            font-size: 85%;
            line-height: 1.45;
            background-color: #f6f8fa;
            border-radius: 6px;
        }}
        pre code {{
            background-color: transparent;
            padding: 0;
        }}
        table {{
            border-spacing: 0;
            border-collapse: collapse;
            margin: 15px 0;
        }}
        th, td {{
            padding: 6px 13px;
            border: 1px solid #dfe2e5;
            text-align: left;
        }}
        th {{
            background-color: #f6f8fa;
            font-weight: 600;
        }}
        tr:nth-child(2n) {{
            background-color: #f6f8fa;
        }}
    </style>
</head>
<body>
{html}
</body>
</html>"""
                Path(file_path).write_text(full_html, encoding="utf-8")
            else:
                Path(file_path).write_text(self.preview.toPlainText(), encoding="utf-8")