"""Modal settings dialog for the embedded proxy.

Edits a :class:`ProxyConfig` and persists via :class:`QSettings` on accept.
The dialog only stores changes in ``self.result_config``; it does not
restart the proxy — the caller decides whether to do so based on the
``changed`` flag returned from :meth:`exec_and_return`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.proxy.proxy_config import ProxyConfig


class ProxySettingsDialog(QDialog):
    """Modal dialog to edit :class:`ProxyConfig`."""

    def __init__(self, current: ProxyConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Proxy Settings")
        self.setModal(True)
        self._original = current

        outer = QVBoxLayout(self)

        intro = QLabel(
            "Configure the embedded Claude Code proxy. The proxy serves a "
            "Claude-compatible API on the configured address. Point "
            "Claude Code CLI at the URL shown in the status bar to use it."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        form = QFormLayout()

        self.host_edit = QLineEdit(current.host)
        self.host_edit.setPlaceholderText("127.0.0.1")
        form.addRow("Bind address:", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        # port=0 is reserved for "OS picks a free port" — not exposed in
        # the UI because the user contract is "Claude Code connects at a
        # known port" and a moving URL is bad UX.
        self.port_spin.setValue(current.port)
        form.addRow("Port:", self.port_spin)

        # Config dir row: line edit + "Browse…" button.
        config_row = QHBoxLayout()
        self.config_dir_edit = QLineEdit(current.config_dir)
        self.config_dir_edit.setPlaceholderText(
            "(auto-discover: data dir, then CWD, then source-proxy location)"
        )
        config_row.addWidget(self.config_dir_edit, 1)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._on_browse)
        config_row.addWidget(self.browse_btn)
        form.addRow("Config dir:", config_row)

        self.autostart_chk = QCheckBox("Start proxy automatically when the app launches")
        self.autostart_chk.setChecked(current.autostart)
        form.addRow("", self.autostart_chk)

        outer.addLayout(form)

        # ---- Per-model endpoint routing --------------------------------
        outer.addWidget(self._h2("API Endpoints"))
        ep_intro = QLabel(
            "Route each Claude Code model to a specific upstream URL. "
            "Requests for models not in the table fall through to the "
            "Default Endpoint below (or to whatever the proxy's config.yaml "
            "specifies when Default is blank)."
        )
        ep_intro.setWordWrap(True)
        outer.addWidget(ep_intro)

        # Default endpoint
        default_row = QHBoxLayout()
        self.default_endpoint_edit = QLineEdit(current.default_endpoint)
        self.default_endpoint_edit.setPlaceholderText(
            "(fall back to provider's configured base_url)"
        )
        default_row.addWidget(self.default_endpoint_edit, 1)
        form_label = QLabel("Default endpoint:")
        outer.addWidget(form_label)
        outer.addLayout(default_row)

        # Model → endpoint table
        self.endpoints_table = QTableWidget(0, 2)
        self.endpoints_table.setHorizontalHeaderLabels(["Model", "Endpoint URL"])
        header = self.endpoints_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.endpoints_table.verticalHeader().setVisible(False)
        self.endpoints_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        for model, endpoint in sorted(current.model_endpoints.items()):
            self._append_endpoint_row(model, endpoint)
        outer.addWidget(self.endpoints_table)

        # Add / Remove buttons
        table_buttons = QHBoxLayout()
        add_btn = QPushButton("Add…")
        add_btn.clicked.connect(self._on_add_endpoint)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._on_remove_endpoint)
        table_buttons.addWidget(add_btn)
        table_buttons.addWidget(remove_btn)
        table_buttons.addStretch()
        outer.addLayout(table_buttons)

        # Hint about config dir
        hint = QLabel(
            "Config dir should contain <code>config.yaml</code> and "
            "<code>.env</code>. Leave blank to auto-discover from the app "
            "data dir, current directory, and common source locations."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        outer.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -- Slots ------------------------------------------------------------
    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select proxy config directory",
            self.config_dir_edit.text() or "",
        )
        if chosen:
            self.config_dir_edit.setText(chosen)

    def _on_add_endpoint(self) -> None:
        self._append_endpoint_row("", "")
        # Edit the new row's model cell immediately.
        row = self.endpoints_table.rowCount() - 1
        self.endpoints_table.setCurrentCell(row, 0)
        self.endpoints_table.editItem(self.endpoints_table.item(row, 0))

    def _on_remove_endpoint(self) -> None:
        rows = sorted(
            {i.row() for i in self.endpoints_table.selectedIndexes()},
            reverse=True,
        )
        for r in rows:
            self.endpoints_table.removeRow(r)

    # -- Helpers ----------------------------------------------------------
    @staticmethod
    def _h2(text: str) -> QLabel:
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _append_endpoint_row(self, model: str, endpoint: str) -> None:
        row = self.endpoints_table.rowCount()
        self.endpoints_table.insertRow(row)
        self.endpoints_table.setItem(row, 0, QTableWidgetItem(model))
        self.endpoints_table.setItem(row, 1, QTableWidgetItem(endpoint))

    # -- Public API -------------------------------------------------------
    def result_config(self) -> ProxyConfig:
        """Return the edited config (valid only after ``exec()`` returns Accepted)."""
        # Read endpoints from the table. Skip rows where either field is
        # blank (the user hasn't finished typing yet).
        model_endpoints: dict[str, str] = {}
        for row in range(self.endpoints_table.rowCount()):
            model_item = self.endpoints_table.item(row, 0)
            endpoint_item = self.endpoints_table.item(row, 1)
            if model_item is None or endpoint_item is None:
                continue
            model = model_item.text().strip()
            endpoint = endpoint_item.text().strip()
            if model and endpoint:
                model_endpoints[model] = endpoint

        return ProxyConfig(
            host=self.host_edit.text().strip() or "127.0.0.1",
            port=int(self.port_spin.value()),
            autostart=self.autostart_chk.isChecked(),
            config_dir=self.config_dir_edit.text().strip(),
            default_endpoint=self.default_endpoint_edit.text().strip(),
            model_endpoints=model_endpoints,
        )

    def exec_and_return(self) -> tuple[ProxyConfig, bool]:
        """Show the dialog and return ``(new_config, changed)``.

        ``changed`` is True iff the user clicked OK AND the resulting
        config differs from the original. Caller uses this to decide
        whether to restart the proxy thread.
        """
        accepted = self.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            return self._original, False
        new_cfg = self.result_config()
        return new_cfg, new_cfg != self._original