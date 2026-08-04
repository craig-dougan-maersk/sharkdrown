"""General tab mixin for the global settings dialog.

Holds the *formerly per-profile* General fields that have been promoted
to global in the menu restructure, plus container runtime settings:

- Proxy bind address (was :attr:`ClaudeProfile.bind_address`)
- Proxy port (was :attr:`ClaudeProfile.port`)
- Proxy config_dir (was :attr:`ClaudeProfile.config_dir`)
- Default session CWD (was :attr:`ClaudeProfile.session_cwd`)
- Container runtime binary (e.g. ``"docker"``, ``"podman"``,
  or an absolute path)
- Default Claude Code container image (per-profile images
  default to this when their ``container_image`` is empty)
- Proxy container image (used when "Run proxy in container"
  is enabled)
- Run proxy in container (checkbox — when True, the proxy
  runs in a container instead of the local ``QThread``)

Editing any of these here applies to every profile and the
embedded proxy — there's a single container runtime choice
for the app.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QToolButton,
    QWidget,
)


class GeneralTabMixin:
    def _build_general_tab(self) -> None:
        """Tab 1: proxy + container-runtime settings."""
        from src.ui.claude._qt_helpers import make_tab

        general_box = QWidget()
        general_layout = QFormLayout(general_box)
        general_layout.setContentsMargins(0, 0, 0, 0)

        self.bind_edit = QLineEdit()
        self.bind_edit.setPlaceholderText(
            "127.0.0.1 (loopback) or 0.0.0.0 (LAN / Docker sessions)"
        )
        general_layout.addRow("Proxy bind address:", self.bind_edit)

        self.port_edit = QSpinBox()
        self.port_edit.setRange(0, 65535)
        self.port_edit.setSpecialValueText("auto (OS picks free port)")
        general_layout.addRow("Proxy port:", self.port_edit)

        config_row = QHBoxLayout()
        self.config_dir_edit = QLineEdit()
        self.config_dir_edit.setPlaceholderText("(auto-discover)")
        config_browse = QToolButton()
        config_browse.setText("Browse…")
        config_browse.clicked.connect(self._browse_config_dir)
        config_row.addWidget(self.config_dir_edit, 1)
        config_row.addWidget(config_browse)
        config_container = QWidget()
        config_container.setLayout(config_row)
        general_layout.addRow("Proxy config dir:", config_container)

        cwd_row = QHBoxLayout()
        self.cwd_edit = QLineEdit()
        self.cwd_edit.setPlaceholderText("(inherit GUI process cwd)")
        cwd_browse = QToolButton()
        cwd_browse.setText("Browse…")
        cwd_browse.clicked.connect(self._browse_cwd)
        cwd_row.addWidget(self.cwd_edit, 1)
        cwd_row.addWidget(cwd_browse)
        cwd_container = QWidget()
        cwd_container.setLayout(cwd_row)
        general_layout.addRow("Default session CWD:", cwd_container)

        # ---- Container runtime section -----------------------------
        # Visual separator: a thin ``QLabel`` with horizontal rule
        # would require a stylesheet; a plain blank row in the
        # form layout is sufficient and theme-friendly.
        general_layout.addRow(self._section_label("Container runtime"))

        runtime_row = QHBoxLayout()
        self.runtime_edit = QLineEdit()
        self.runtime_edit.setPlaceholderText(
            "docker (default), podman, or absolute path e.g. /usr/local/bin/docker"
        )
        runtime_browse = QToolButton()
        runtime_browse.setText("Browse…")
        runtime_browse.clicked.connect(self._browse_runtime)
        runtime_row.addWidget(self.runtime_edit, 1)
        runtime_row.addWidget(runtime_browse)
        runtime_container = QWidget()
        runtime_container.setLayout(runtime_row)
        general_layout.addRow("Container runtime binary:", runtime_container)

        self.claude_image_edit = QLineEdit()
        self.claude_image_edit.setPlaceholderText("claude-code:latest")
        general_layout.addRow(
            "Default Claude Code image:", self.claude_image_edit
        )

        self.proxy_image_edit = QLineEdit()
        self.proxy_image_edit.setPlaceholderText("sharkdrown-proxy:latest")
        general_layout.addRow("Proxy image:", self.proxy_image_edit)

        self.run_proxy_in_container_check = QCheckBox(
            "Run proxy in container (requires sharkdrown-proxy:latest)"
        )
        general_layout.addRow("", self.run_proxy_in_container_check)

        self.tabs.addTab(make_tab(general_box), "General")

    def _populate_general_tab(self) -> None:
        self.bind_edit.setText(self._settings.bind_address or "127.0.0.1")
        self.port_edit.setValue(int(self._settings.port or 0))
        self.config_dir_edit.setText(self._settings.config_dir or "")
        self.cwd_edit.setText(self._settings.session_cwd or "")
        self.runtime_edit.setText(self._settings.container_runtime or "docker")
        self.claude_image_edit.setText(
            self._settings.default_claude_image or "claude-code:latest"
        )
        self.proxy_image_edit.setText(
            self._settings.proxy_image or "sharkdrown-proxy:latest"
        )
        self.run_proxy_in_container_check.setChecked(
            bool(self._settings.run_proxy_in_container)
        )

    def _collect_general_tab(self) -> None:
        self._settings.bind_address = (
            self.bind_edit.text().strip() or "127.0.0.1"
        )
        self._settings.port = int(self.port_edit.value())
        self._settings.config_dir = self.config_dir_edit.text().strip()
        cwd = self.cwd_edit.text().strip()
        self._settings.session_cwd = cwd or None
        # Container runtime — strip surrounding whitespace; fall
        # back to ``"docker"`` when blank so a half-typed path
        # doesn't break every subsequent launch.
        self._settings.container_runtime = (
            self.runtime_edit.text().strip() or "docker"
        )
        self._settings.default_claude_image = (
            self.claude_image_edit.text().strip() or "claude-code:latest"
        )
        self._settings.proxy_image = (
            self.proxy_image_edit.text().strip() or "sharkdrown-proxy:latest"
        )
        self._settings.run_proxy_in_container = (
            self.run_proxy_in_container_check.isChecked()
        )

    def _section_label(self, text: str) -> QLabel:
        """Render a bold sub-heading inside a form layout."""
        lbl = QLabel(f"<b>{text}</b>")
        lbl.setStyleSheet("margin-top: 8px;")
        return lbl

    def _browse_config_dir(self) -> None:
        """Show a directory picker for the proxy's config dir."""
        start = self.config_dir_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Proxy config directory", start
        )
        if chosen:
            self.config_dir_edit.setText(chosen)

    def _browse_cwd(self) -> None:
        start = self.cwd_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Default session working directory", start
        )
        if chosen:
            self.cwd_edit.setText(chosen)

    def _browse_runtime(self) -> None:
        """Show a file picker for the container runtime binary."""
        start = self.runtime_edit.text() or "/usr/local/bin/docker"
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Container runtime binary", start
        )
        if chosen:
            self.runtime_edit.setText(chosen)