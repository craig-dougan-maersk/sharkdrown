from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QWidget,
)


class GeneralTabMixin:
    def _build_general_tab(self) -> None:
        """Tab 1: name, default model, proxy bind/port/config_dir, session CWD.

        The proxy / network fields live here because they're identity-
        adjacent — the bind/port/config_dir determine *which* proxy the
        profile controls, alongside the profile's own name and default
        model. This was the original placement in the linear-flow dialog;
        tabbing just relocates the same fields into one panel rather
        than splitting them across "General" and "Network".
        """
        general_box = QWidget()
        general_layout = QFormLayout(general_box)
        general_layout.setContentsMargins(0, 0, 0, 0)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Default")
        general_layout.addRow("Name:", self.name_edit)

        # Default model — a ``QComboBox`` populated from the alias list,
        # never free-text. Aliases are the user-facing identity of a
        # model (the real upstream IDs like ``MiniMax-M3`` stay
        # hidden in the Model Configuration table); the proxy's
        # alias-rewrite layer translates the chosen alias back to
        # the real name at request time.
        #
        # Items added at populate-time:
        #   - ``(no default)``  — when ``default_model`` is unset
        #     (``userData = None``).
        #   - every deduped alias across all endpoint rows
        #     (``userData = alias_string``).
        #   - ``(real model: X)`` — fallback for legacy profiles whose
        #     ``default_model`` was a real upstream ID; selecting
        #     this preserves the verbatim value through save (the
        #     proxy still accepts a real name if the user prefers).
        self.default_model_combo = QComboBox()
        self.default_model_combo.setEditable(False)
        general_layout.addRow("Default model:", self.default_model_combo)

        # ---- Proxy / network settings ----------------------------------
        # Bind address + port + config_dir moved off ``ProxyConfig``
        # onto the profile — the proxy's network identity is now a
        # per-profile concern, so switching active profile restarts
        # the proxy with new host/port/config-dir.
        self.bind_edit = QLineEdit()
        self.bind_edit.setPlaceholderText("127.0.0.1 (loopback) or 0.0.0.0 (LAN)")
        general_layout.addRow("Proxy bind address:", self.bind_edit)

        self.port_edit = QSpinBox()
        self.port_edit.setRange(0, 65535)
        # ``0`` means "let the OS pick a free port" — the actual bound
        # port is reported via ``ProxyServerThread.started_ok``.
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
        general_layout.addRow("Session CWD:", cwd_container)

        self.tabs.addTab(self._make_tab(general_box), "General")

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(f"<b>{text}</b>")
        lbl.setStyleSheet("margin-top: 8px;")
        return lbl

    def _wrap_in_scroll(self, widget: QWidget, *, max_height: int) -> QScrollArea:
        """Wrap a widget in a fixed-height ``QScrollArea``.

        The two key/value tables in this dialog (Model Endpoints,
        Environment Variables) can grow unboundedly — a profile with
        10+ model entries would push the rest of the dialog content
        off-screen. Wrapping each table in a scroll area with a
        ``max_height`` cap means more than ~2 rows are reachable via
        scroll, but the dialog layout itself stays compact.

        ``setWidgetResizable(True)`` is required so the inner widget
        is sized to the scroll area's width; ``setMaximumHeight`` is
        what limits how much vertical real estate the scroll region
        takes before its own scrollbar appears.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        scroll.setMaximumHeight(max_height)
        # Hide the frame so the table blends into the surrounding
        # dialog — without this, a 1px grey border makes the scroll
        # region look like a separate panel.
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

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
        chosen = QFileDialog.getExistingDirectory(self, "Session working directory", start)
        if chosen:
            self.cwd_edit.setText(chosen)
