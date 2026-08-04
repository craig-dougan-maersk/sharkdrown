"""Container tab — per-profile Docker container settings.

Each profile can opt to run its Claude Code sessions inside a
per-profile container, bind-mounted against a scoped
``~/.claude`` directory. The Container tab exposes three fields:

- **Run in container** (``run_in_container``) — master toggle.
  When True, ``ClaudeContainerView._launch_session`` routes the
  session through ``DockerSession`` (which ``docker exec -i``s
  the binary in the named container) instead of the default
  local PTY.
- **Container image** (``container_image``) — image tag used
  for ``docker create``. Empty value falls back to the global
  default (``Claude > Settings > Container Runtime > Default
  Claude Code image``).
- **Container name** (``container_name``) — used as
  ``docker --name``. Empty value falls back to the profile
  slug at session-launch time.

The image and name fields are disabled while ``run_in_container``
is unchecked — there's no point surfacing values the launcher
won't read.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)


class ContainerTabMixin:
    def _build_container_tab(self) -> None:
        """Tab: per-profile container settings."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab) if False else None  # placeholder
        from PySide6.QtWidgets import QVBoxLayout

        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        help_label = QLabel(
            "Run this profile's Claude Code sessions inside a "
            "Docker (or Podman) container. The container's "
            "<code>~/.claude</code> is bind-mounted from the "
            "profile-scoped copy under "
            "<code>&lt;data_dir&gt;/profiles/&lt;slug&gt;/.claude/</code> "
            "so settings, agents, and plugins stay scoped per "
            "profile. Leave <b>Run in container</b> unchecked to "
            "launch via the local PTY (no Docker required)."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        tab_layout.addWidget(help_label)

        form_box = QWidget()
        form = QFormLayout(form_box)
        form.setContentsMargins(0, 0, 0, 0)

        self.run_in_container_check = QCheckBox(
            "Run sessions in a container"
        )
        form.addRow(self.run_in_container_check)

        # Container image — QLineEdit so the user can type any
        # tag (including digests). The dialog uses the global
        # ``default_claude_image`` as the placeholder hint.
        self.container_image_edit = QLineEdit()
        self.container_image_edit.setPlaceholderText(
            "(use default from Claude > Settings)"
        )
        form.addRow("Container image:", self.container_image_edit)

        # Container name — leave blank to default to the slug.
        self.container_name_edit = QLineEdit()
        self.container_name_edit.setPlaceholderText("(default: profile slug)")
        form.addRow("Container name:", self.container_name_edit)

        # Reset-to-defaults button. Resets the per-profile image
        # and name to empty so they fall back to the global
        # defaults at session-launch time.
        reset_row = QHBoxLayout()
        reset_btn = QPushButton("Reset to defaults")
        reset_btn.clicked.connect(self._on_container_reset)
        reset_row.addStretch(1)
        reset_row.addWidget(reset_btn)
        reset_container = QWidget()
        reset_container.setLayout(reset_row)
        form.addRow("", reset_container)

        # Status hint — reads the resolved values from the global
        # store so the user can see what would actually be used.
        self.container_status = QLabel("")
        self.container_status.setStyleSheet(
            "color: gray; font-size: 11px;"
        )
        self.container_status.setWordWrap(True)
        form.addRow("", self.container_status)

        tab_layout.addWidget(form_box)
        tab_layout.addStretch(1)

        self.run_in_container_check.toggled.connect(self._on_run_in_container_toggled)
        self.container_image_edit.textChanged.connect(self._refresh_container_status)
        self.container_name_edit.textChanged.connect(self._refresh_container_status)

        self.tabs.addTab(tab, "Container")

    def _populate_container_tab(self) -> None:
        self.run_in_container_check.setChecked(bool(
            getattr(self._profile, "run_in_container", True)
        ))
        self.container_image_edit.setText(
            getattr(self._profile, "container_image", "") or ""
        )
        self.container_name_edit.setText(
            getattr(self._profile, "container_name", "") or ""
        )
        self._on_run_in_container_toggled(self.run_in_container_check.isChecked())
        self._refresh_container_status()

    def _collect_container_tab(self) -> None:
        self._profile.run_in_container = (
            self.run_in_container_check.isChecked()
        )
        # Empty strings mean "fall back to global default at
        # launch time" — never persist a meaningless value.
        self._profile.container_image = (
            self.container_image_edit.text().strip()
        )
        self._profile.container_name = (
            self.container_name_edit.text().strip()
        )

    def _on_run_in_container_toggled(self, checked: bool) -> None:
        """Disable image/name fields when the master toggle is off."""
        self.container_image_edit.setEnabled(checked)
        self.container_name_edit.setEnabled(checked)
        self._refresh_container_status()

    def _on_container_reset(self) -> None:
        self.container_image_edit.clear()
        self.container_name_edit.clear()
        self._refresh_container_status()

    def _refresh_container_status(self) -> None:
        """Surface the resolved values that ``_launch_session`` will use.

        Reads from the live global settings via
        ``self._global_settings_for_validation()`` so the user
        sees the *resolved* image / runtime binary (not the
        raw placeholder) before they save.
        """
        settings = self._global_settings_for_validation()
        runtime = settings.container_runtime or "docker"
        default_image = settings.default_claude_image or "claude-code:latest"
        image = (
            self.container_image_edit.text().strip()
            or default_image
        )
        name = (
            self.container_name_edit.text().strip()
            or getattr(self._profile, "slug", "profile")
        )
        bits = [
            f"Runtime: <code>{runtime}</code>",
            f"Image: <code>{image}</code>",
            f"Container: <code>{name}</code>",
        ]
        self.container_status.setText("  •  ".join(bits))