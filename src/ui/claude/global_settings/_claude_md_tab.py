"""CLAUDE.md tab — edit the user's global ``~/.claude/CLAUDE.md``.

The file is a plain Markdown document with no frontmatter — Claude
Code reads it as instructions whenever a session starts in any
profile. On profile-scope creation we prepend its content into the
profile's scoped ``CLAUDE.md`` (see ``src/ui/claude/docker_claude_md.py``),
so the global rules lead and the profile-specific ones follow.

Editing pattern is the same as the profile editor's Settings.JSON tab:
monospace ``QPlainTextEdit``, Save / Revert buttons, status label
showing the resolved file path so the user knows exactly which file
they're editing. On Save we atomically write with
``tempfile.mkstemp`` + ``os.replace`` + ``os.fchmod(fd, 0o600)`` on
POSIX — the file may carry secrets, and Claude Code tolerates 0o600
file perms without complaint.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.global_settings import GLOBAL_CLAUDE_MD_PATH

_IS_POSIX = sys.platform != "win32"


class ClaudeMdTabMixin:
    def _build_claude_md_tab(self) -> None:
        """Tab 4: global CLAUDE.md editor."""
        from src.ui.claude._qt_helpers import make_tab

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)

        help_label = QLabel(
            "Edit your global <code>~/.claude/CLAUDE.md</code>. This content "
            "is auto-prepended to every profile's scoped CLAUDE.md on session "
            "start, so changes here apply to all Claude Code sessions after "
            "the next launch."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(help_label)

        path_label = QLabel(f"Path: <code>{GLOBAL_CLAUDE_MD_PATH}</code>")
        from PySide6.QtCore import Qt
        path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        path_label.setStyleSheet("color: gray; font-size: 11px;")
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        self.claude_md_edit = QPlainTextEdit()
        mono = self.claude_md_edit.font()
        mono.setFamily(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        )
        self.claude_md_edit.setFont(mono)
        self.claude_md_edit.setPlaceholderText(
            "# Global rules for Claude Code\n\nThese rules apply to every profile.\n"
        )
        layout.addWidget(self.claude_md_edit, 1)

        btn_row = QHBoxLayout()
        self.claude_md_revert_btn = QPushButton("Revert")
        self.claude_md_revert_btn.clicked.connect(self._on_claude_md_revert)
        self.claude_md_save_btn = QPushButton("Save")
        self.claude_md_save_btn.setDefault(True)
        self.claude_md_save_btn.clicked.connect(self._on_claude_md_save)
        btn_row.addStretch(1)
        btn_row.addWidget(self.claude_md_revert_btn)
        btn_row.addWidget(self.claude_md_save_btn)
        btn_container = QWidget()
        btn_container.setLayout(btn_row)
        layout.addWidget(btn_container)

        self.claude_md_status = QLabel("")
        self.claude_md_status.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.claude_md_status)

        self.tabs.addTab(make_tab(tab), "CLAUDE.md")

    def _populate_claude_md_tab(self) -> None:
        """Load ``~/.claude/CLAUDE.md`` into the editor.

        Missing file → empty editor + "file does not exist" status.
        Existing file → load contents verbatim, status shows last-
        modified time so the user knows what they're looking at.
        """
        path = GLOBAL_CLAUDE_MD_PATH
        if not path.is_file():
            self.claude_md_edit.setPlainText("")
            self.claude_md_status.setText("(file does not exist yet)")
            self.claude_md_revert_btn.setEnabled(False)
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.claude_md_edit.setPlainText("")
            self.claude_md_status.setText(f"Read error: {exc}")
            self.claude_md_revert_btn.setEnabled(False)
            return
        self.claude_md_edit.setPlainText(content)
        try:
            mtime = path.stat().st_mtime
            from datetime import datetime

            stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            self.claude_md_status.setText(f"Loaded (last modified {stamp})")
        except OSError:
            self.claude_md_status.setText("Loaded")
        self.claude_md_revert_btn.setEnabled(False)

    def _on_claude_md_revert(self) -> None:
        self._populate_claude_md_tab()

    def _on_claude_md_save(self) -> None:
        """Write the editor contents to ``~/.claude/CLAUDE.md`` atomically."""
        path = GLOBAL_CLAUDE_MD_PATH
        content = self.claude_md_edit.toPlainText()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, content)
        except OSError as exc:
            self.claude_md_status.setText(f"Save failed: {exc}")
            return
        try:
            mtime = path.stat().st_mtime
            from datetime import datetime

            stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            self.claude_md_status.setText(f"Saved (last modified {stamp})")
        except OSError:
            self.claude_md_status.setText("Saved")
        self.claude_md_revert_btn.setEnabled(False)

    def _collect_claude_md_tab(self) -> None:
        """No-op placeholder — the CLAUDE.md editor writes immediately
        on Save (not deferred to OK) because the user expects the
        global file to be on disk the moment they click Save.
        """


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically with chmod 600 (POSIX).

    Mirrors :meth:`src.profiles.store.ProfileStore._save_raw` —
    ``tempfile.mkstemp`` + ``os.fchmod`` + ``os.replace`` so a crash
    mid-write can't truncate the existing file and a chmod-race
    window (file briefly world-readable between close and chmod)
    can't expose secrets.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        suffix=".tmp",
        prefix=path.stem + "_",
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
            if _IS_POSIX:
                fh.flush()
                os.fsync(fh.fileno())
                os.fchmod(fh.fileno(), 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise