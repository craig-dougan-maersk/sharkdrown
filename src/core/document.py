"""
Document model for the editor.

This module provides the DocumentTab class which represents
a document being edited in the application.
"""

from pathlib import Path


class DocumentTab:
    """Represents a tab in the editor with its content and type."""

    def __init__(
        self,
        file_path: Path | None = None,
        content: str = "",
        format_type: str | None = None,
    ):
        self.file_path = file_path
        self.content = content
        self.display_name: str | None = None
        self.cursor_block: int = 0
        self.cursor_column: int = 0

        if format_type is not None:
            self.format_type = format_type
        elif file_path:
            self.format_type = self._detect_format()
        else:
            self.format_type = "markdown"

    def _detect_format(self) -> str:
        """Detect file format from extension."""
        if self.file_path is None:
            return "markdown"
        suffix = self.file_path.suffix.lower()
        format_map = {
            ".md": "markdown",
            ".markdown": "markdown",
            ".json": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".txt": "text",
        }
        return format_map.get(suffix, "text")

    def title(self) -> str:
        """Get tab title, showing filename or display_name or 'Untitled'."""
        if self.display_name:
            return self.display_name
        if self.file_path:
            return self.file_path.name or "Untitled"
        return "Untitled"

    def is_modified(self) -> bool:
        """Check if content has been modified."""
        if self.file_path is None:
            return bool(self.content)
        try:
            return self.content != self.file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return bool(self.content)

    def load_content(self) -> str:
        """Load content from file."""
        if self.file_path and self.file_path.exists():
            return self.file_path.read_text(encoding="utf-8")
        return self.content

    def save(self) -> bool:
        """Save content to file."""
        if self.file_path is None:
            return False
        try:
            self.file_path.write_text(self.content, encoding="utf-8")
            return True
        except OSError:
            return False
