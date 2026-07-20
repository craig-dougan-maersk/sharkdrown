"""Embedded dashboard view backed by QWebEngineView.

The dashboard is served by the embedded proxy at ``/`` (rendered by the
proxy's Jinja2 templates + htmx + Tailwind Play CDN). This widget simply
loads that URL once the proxy signals it's listening.

The QWebEngineView import is deferred to ``__init__`` because:
  1. ``QWebEngineWidgets`` is a heavy C++-backed module — about 80 MB of
     binaries on first import.
  2. It must be imported after a QApplication has been created, otherwise
     Qt raises "QtWebEngineWidgets must be imported before a QCoreApplication
     is created" at first use.

The widget layout is intentionally minimal: a single dropdown ``QToolButton``
in the top-right corner holds all actions (Reload, Open in Browser, Copy
URL). This avoids wasting vertical space on a toolbar with two buttons when
the dashboard is shown full-window as a document tab.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class DashboardView(QWidget):
    """Hosts the proxy's HTML dashboard inside the desktop window.

    Signals
    -------
    url_loaded(str):
        Emitted once the WebView has loaded the dashboard URL. Useful for
        "loaded successfully" status messages.
    """

    url_loaded = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Lazy import — QtWebEngineWidgets must come after QApplication.
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._base_url: str = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Compact top bar — URL on the left, dropdown menu on the right.
        # The WebView fills the rest of the widget.
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        bar.setSpacing(6)

        self.url_label = QLabel("(no URL loaded)")
        self.url_label.setStyleSheet("color: gray;")
        bar.addWidget(self.url_label, 1)  # stretch

        self.menu_btn = QToolButton()
        self.menu_btn.setText("Actions")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setStyleSheet(
            "QToolButton { padding: 2px 10px; }"
        )
        self._build_menu()
        bar.addWidget(self.menu_btn, 0)

        outer.addLayout(bar)

        # The web view itself — takes all remaining vertical space.
        self.view = QWebEngineView()
        self.view.urlChanged.connect(self._on_url_changed)
        outer.addWidget(self.view, 1)

    # -- Menu construction ------------------------------------------------
    def _build_menu(self) -> None:
        menu = QMenu(self.menu_btn)

        self.reload_action = QAction("Reload", menu)
        self.reload_action.triggered.connect(self._reload)
        menu.addAction(self.reload_action)

        self.open_external_action = QAction("Open in Browser", menu)
        self.open_external_action.triggered.connect(self._open_external)
        menu.addAction(self.open_external_action)

        menu.addSeparator()

        self.copy_url_action = QAction("Copy URL", menu)
        self.copy_url_action.triggered.connect(self._copy_url)
        menu.addAction(self.copy_url_action)

        self.menu_btn.setMenu(menu)

    # -- Public API -------------------------------------------------------
    def set_base_url(self, url: str) -> None:
        """Point the WebView at a new proxy URL.

        Typically called from ``MainWindow._on_proxy_started`` with
        ``f"http://{host}:{port}"``. Triggers an immediate ``view.load``.
        """
        self._base_url = url.rstrip("/")
        self.url_label.setText(self._base_url)
        self.view.load(QUrl(f"{self._base_url}/"))

    def base_url(self) -> str:
        return self._base_url

    # -- Slots ------------------------------------------------------------
    def _reload(self) -> None:
        if self._base_url:
            self.view.load(QUrl(f"{self._base_url}/"))

    def _open_external(self) -> None:
        if self._base_url:
            QDesktopServices.openUrl(QUrl(self._base_url))

    def _copy_url(self) -> None:
        if not self._base_url:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._base_url)

    def _on_url_changed(self, url: QUrl) -> None:
        # ``urlChanged`` fires on every navigation; we only care about the
        # first successful load of the dashboard root.
        if url.toString().rstrip("/") == self._base_url:
            self.url_loaded.emit(self._base_url)