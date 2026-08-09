"""Env Vars tab mixin for the global settings dialog.

Edits ``<data_dir>/defaults.env`` — the user-editable global Claude
Code session env file deployed on first launch from
``src/profiles/resources/defaults.env``.

The tab shows the raw ``KEY=VALUE`` text (one per line, ``#`` for
comments) in a :class:`QPlainTextEdit`, plus a "Reset to defaults"
button that re-copies the packaged template. On accept, the edited
text is parsed by :func:`src.profiles.global_env.parse_env_file` and
written back via :func:`src.profiles.global_env.save_global_env`
(atomic write + chmod 600).

Why a raw-text edit rather than key/value rows
----------------------------------------------
Claude Code's env var surface grows over time (``CLAUDE_CODE_*``
env vars are added with each release). A raw-text editor lets the
user add new vars without the app catching up; a structured
key/value row UI would silently drop unknown vars. We still parse
on save so malformed lines surface a warning rather than a silent
no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles import global_env

logger = logging.getLogger(__name__)


class EnvVarsTabMixin:
    def _build_env_vars_tab(self) -> None:
        """Tab: editable list of global Claude Code session env vars.

        Persisted to ``<data_dir>/defaults.env``. The text edit shows
        the raw ``KEY=VALUE`` lines (one per line); Save commits via
        :func:`save_global_env` which atomically writes the file with
        ``chmod 600``.
        """
        from src.ui.claude._qt_helpers import make_tab

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header — explain what this tab edits.
        header = QLabel(
            "Global environment variables delivered to every Claude Code "
            "session launched from this app.\n"
            "Edit values as ``KEY=VALUE`` (one per line). Lines beginning "
            "with ``#`` are comments and ignored."
        )
        header.setWordWrap(True)
        header.setStyleSheet("color: gray;")
        layout.addWidget(header)

        # Plain-text editor — same approach as the CLAUDE.md tab.
        # Monospace so the user can visually scan aligned ``=`` columns.
        self.env_vars_edit = QPlainTextEdit()
        self.env_vars_edit.setPlaceholderText(
            "# KEY=VALUE per line\n"
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1\n"
            "CLAUDE_CODE_NO_FLICKER=1\n"
        )
        font = self.env_vars_edit.font()
        font.setFamily("Menlo,Consolas,monospace")
        self.env_vars_edit.setFont(font)
        layout.addWidget(self.env_vars_edit, 1)

        # Footer — Reset button + path hint.
        footer_row = QHBoxLayout()
        self.env_vars_path_label = QLabel("")
        self.env_vars_path_label.setStyleSheet("color: gray; font-size: 11px;")
        self.env_vars_path_label.setWordWrap(True)
        footer_row.addWidget(self.env_vars_path_label, 1)

        reset_btn = QPushButton("Reset to defaults")
        reset_btn.setToolTip(
            "Re-copy the packaged defaults.env over the deployed file. "
            "Any customisations will be lost."
        )
        reset_btn.clicked.connect(self._on_env_vars_reset)
        footer_row.addWidget(reset_btn)

        layout.addLayout(footer_row)

        self.tabs.addTab(make_tab(page), "Env Vars")

    def _populate_env_vars_tab(self) -> None:
        """Read ``<data_dir>/defaults.env`` and show it in the editor.

        ``ensure_global_env_file`` is idempotent — if the file doesn't
        exist (very first launch, before :func:`main` runs), this
        deploys the shipped template first. In practice the dialog is
        only opened after :func:`main` has run, so the file already
        exists; ``ensure`` is a belt-and-braces guard.
        """
        data_dir = self._data_dir()
        global_env.ensure_global_env_file(data_dir)
        path = data_dir / global_env.GLOBAL_ENV_FILENAME
        self.env_vars_path_label.setText(f"<i>{path}</i>")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            text = ""
        self.env_vars_edit.setPlainText(text)

    def _collect_env_vars_tab(self) -> None:
        """Parse the editor text and atomically write it back to disk.

        The parser tolerates malformed lines (logs a warning, skips
        the line) so a typo doesn't fail the save; the entire dialog
        accept still succeeds even if one line is broken.
        """
        data_dir = self._data_dir()
        text = self.env_vars_edit.toPlainText()
        # Write to a temp file in-memory first so ``parse_env_file``
        # sees the user's exact text (the editor's ``.env`` parsing
        # would otherwise race with a concurrent ``save_global_env``
        # writing to disk).
        try:
            parsed = self._parse_with_warnings(text)
        except Exception as exc:  # never fail the dialog accept
            logger.warning("Env-vars parse failed; skipping save: %s", exc)
            return
        try:
            global_env.save_global_env(data_dir, parsed)
        except OSError as exc:
            logger.warning("Could not save %s: %s", data_dir, exc)

    def _on_env_vars_reset(self) -> None:
        """Re-copy the packaged ``defaults.env`` template to disk.

        Reloads the editor text after the copy so the user sees the
        shipped defaults. Existing user edits are overwritten; the
        file's ``chmod 600`` is preserved by ``ensure_global_env_file``.
        """
        data_dir = self._data_dir()
        # Delete so ``ensure_global_env_file`` re-deploys from the
        # packaged template rather than treating the user's file as
        # authoritative.
        path = data_dir / global_env.GLOBAL_ENV_FILENAME
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        global_env.ensure_global_env_file(data_dir)
        self._populate_env_vars_tab()

    def _parse_with_warnings(self, text: str) -> dict[str, str]:
        """Parse the editor's text using the same logic as the file
        parser, but accept a string (the editor's content) rather
        than a path."""
        out: dict[str, str] = {}
        import re as _re

        line_re = _re.compile(
            r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
        )
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = line_re.match(raw)
            if not m:
                logger.warning("Ignoring malformed env-vars line: %r", raw)
                continue
            out[m.group(1)] = m.group(2)
        return out

    def _data_dir(self) -> Path:
        """Resolve the app data directory.

        The dialog may not always have a ``_data_dir`` attribute; we
        fall back to ``src.proxy.paths.data_dir()`` so the tab works
        regardless of how the dialog is constructed.
        """
        cached = getattr(self, "_env_vars_data_dir", None)
        if cached is not None:
            return cached
        from src.proxy.paths import data_dir

        d = data_dir()
        self._env_vars_data_dir = d
        return d