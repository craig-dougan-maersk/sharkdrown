"""MainWindow — thin shell composing per-concern mixins.

The window itself owns:
- __init__ (state setup)
- _init_central_widget (top-level tab widget, splitter, containers)
- _on_view_tab_changed (focus transfer between top-level tabs)
- closeEvent (orderly shutdown)

All menu, document, proxy, and profile logic lives in the mixin modules.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QMainWindow,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.config import APP_TITLE
from src.profiles.store import ProfileStore
from src.proxy.proxy_config import ProxyConfig
from src.proxy.server_thread import ProxyServerThread
from src.ui.claude.claude_container_view import ClaudeContainerView
from src.ui.terminal.terminal_container_view import TerminalContainerView

from src.ui.main_window._document_ops import DocumentOpsMixin
from src.ui.main_window._menu_construction import MenuConstructionMixin
from src.ui.main_window._profile_management import ProfileManagementMixin
from src.ui.main_window._proxy_lifecycle import ProxyLifecycleMixin


class MainWindow(
    DocumentOpsMixin,
    MenuConstructionMixin,
    ProfileManagementMixin,
    ProxyLifecycleMixin,
    QMainWindow,
):
    """Main application window with tabbed split view."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1200, 800)

        # State management
        self.split_enabled = False
        self.preview_visible = False
        self._tab_pairs: dict[int, int] = {}
        self._preview_docs: dict[int, object] = {}

        # Embedded proxy state
        self.proxy_thread: ProxyServerThread | None = None
        self.proxy_port: int = 0
        self.proxy_host: str = "127.0.0.1"
        self.activity_tile = None
        self.terminal_container: TerminalContainerView | None = None
        self.claude_container: ClaudeContainerView | None = None
        self.profile_store: ProfileStore | None = None
        self._proxy_autostart_fired = False
        self._restart_after_stop = False

        # Initialize components
        self._init_central_widget()
        self._init_menu()
        self._init_code_menu()
        self._init_terminal_menu()
        self._init_claude_menu()
        self._init_status_bar()

        # Install double-click rename on tab bar
        self._install_tab_rename_filter()

        # Create initial tab — named ``untitled.md`` (not the generic
        # ``Untitled``) so the format tag and filename are immediately
        # informative in the tab strip.
        self._new_untitled_tab("markdown")

        self._apply_view_state()

        self._sync_format_combo_to_current_document()
        self._update_status_bar_visibility(self._TAB_INDEX_DOCUMENTS)

        # Honour the user's autostart preference
        cfg = ProxyConfig.load()
        if cfg.autostart and not self._proxy_autostart_fired:
            self._proxy_autostart_fired = True
            QTimer.singleShot(0, self._start_server)

    def _init_central_widget(self) -> None:
        """Initialize the central widget with top-level tab structure."""
        from src.ui.widgets.markdown_toolbar import MarkdownToolbar
        from src.ui.widgets.lint_panel import LintPanel

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        self.editor_tabs = QTabWidget()
        self.editor_tabs.setTabsClosable(True)
        self.editor_tabs.setMovable(True)
        self.editor_tabs.tabCloseRequested.connect(self._close_tab)
        self.editor_tabs.currentChanged.connect(self._on_editor_tab_changed)

        self.preview_tabs = QTabWidget()
        self.preview_tabs.setTabsClosable(True)
        self.preview_tabs.setMovable(True)
        self.preview_tabs.tabCloseRequested.connect(self._close_preview_tab)
        self.preview_tabs.currentChanged.connect(self._on_preview_tab_changed)

        self.splitter.addWidget(self.editor_tabs)
        self.splitter.addWidget(self.preview_tabs)

        # Markdown toolbar (hidden by default, shown for markdown tabs)
        self.markdown_toolbar = MarkdownToolbar()
        self.markdown_toolbar.setVisible(False)

        # Lint panel (below editor, hidden by default)
        self.lint_panel = LintPanel()
        self.lint_panel.setVisible(False)
        self.lint_panel.issue_clicked.connect(self._on_lint_issue_clicked)

        # Vertical splitter: editor area on top, lint panel on bottom
        self.doc_vsplitter = QSplitter(Qt.Vertical)
        self.doc_vsplitter.addWidget(self.splitter)
        self.doc_vsplitter.addWidget(self.lint_panel)
        self.doc_vsplitter.setStretchFactor(0, 4)
        self.doc_vsplitter.setStretchFactor(1, 1)

        # ---- Top-level view tabs (Documents / Terminals / Claude) ------
        self.view_tabs = QTabWidget()
        self.view_tabs.setDocumentMode(True)
        self.view_tabs.setTabsClosable(False)
        self.view_tabs.setMovable(False)

        self._TAB_INDEX_DOCUMENTS = 0
        self._TAB_INDEX_TERMINALS = 1
        self._TAB_INDEX_CLAUDE = 2

        # Page 1: Documents
        documents_page = QWidget()
        documents_layout = QVBoxLayout(documents_page)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        documents_layout.setSpacing(0)
        documents_layout.addWidget(self.markdown_toolbar)
        documents_layout.addWidget(self.doc_vsplitter)
        self.view_tabs.addTab(documents_page, "Documents")
        self.view_tabs.currentChanged.connect(self._on_view_tab_changed)

        # Page 2: Terminals
        self.terminal_container = TerminalContainerView()
        self.view_tabs.addTab(self.terminal_container, "Terminals")

        # Page 3: Claude
        self.claude_container = ClaudeContainerView()
        self.claude_container.set_proxy_callbacks(
            is_proxy_running=self._is_proxy_running,
            get_proxy_base_url=self._proxy_base_url,
            request_proxy_start=self._request_proxy_start,
        )
        self.claude_container.proxy_requested.connect(self._on_claude_proxy_requested)
        self.view_tabs.addTab(self.claude_container, "Claude")

        self.setCentralWidget(self.view_tabs)

    def _on_view_tab_changed(self, index: int) -> None:
        """Sync focus and status bar when the top-level tab changes."""
        self._update_status_bar_visibility(index)

        if index == getattr(self, "_TAB_INDEX_TERMINALS", -1):
            if self.terminal_container is not None:
                self.terminal_container.focus_active_session()
        if index == getattr(self, "_TAB_INDEX_CLAUDE", -1):
            if self.claude_container is not None:
                self.claude_container.focus_active_session()

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Stop the proxy thread cleanly before the window closes."""
        thread = self.proxy_thread
        if self.activity_tile is not None:
            self.activity_tile.stop()
        if self.terminal_container is not None:
            self.terminal_container.shutdown()
        if self.claude_container is not None:
            self.claude_container.shutdown()
        if thread is not None and thread.isRunning():
            thread.request_stop()
            if not thread.wait(3000):
                thread.terminate()
                thread.wait(1000)
        super().closeEvent(event)
