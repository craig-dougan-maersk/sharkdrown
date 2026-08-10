"""MainWindow — thin shell composing per-concern mixins.

The window itself owns:
- __init__ (state setup)
- _init_central_widget (top-level tab widget, splitter, containers)
- _on_view_tab_changed (focus transfer between top-level tabs)
- closeEvent (orderly shutdown)

All menu, document, proxy, and profile logic lives in the mixin modules.

View selector (post-refactor)
-----------------------------
The four top-level pages (Text / Diagrams / Terminals / Claude) are
stacked in a ``QStackedWidget`` and selected by a ``QComboBox``
sitting in the menu bar's right corner via
``QMenuBar.setCornerWidget``. The combo's index is the same as the
old tab indices (``_TAB_INDEX_*``) so all the existing dispatch
helpers (``_focus_terminals_tab`` etc.) keep working unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QMainWindow,
    QMenuBar,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.config import APP_TITLE
from src.profiles.store import ProfileStore
from src.proxy.proxy_config import ProxyConfig
from src.proxy.server_thread import ProxyServerThread
from src.ui.claude.claude_container_view import ClaudeContainerView
from src.ui.diagrams.diagram_container_view import DiagramContainerView
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
        self.diagram_container: DiagramContainerView | None = None
        self.profile_store: ProfileStore | None = None
        self._proxy_autostart_fired = False
        self._restart_after_stop = False

        # Initialize components
        # NOTE: menu-bar order is *call* order — QMenuBar.addMenu appends.
        # Main (with its new Settings submenu) comes from _init_menu;
        # the rest follow in sequence, giving:
        # Main | Text | Diagrams | Terminal | Claude,
        # plus a right-aligned view-selector combo via setCornerWidget.
        self._init_central_widget()
        self._init_menu()
        self._init_text_menu()
        self._init_diagrams_menu()
        self._init_terminal_menu()
        self._init_claude_menu()
        self._init_status_bar()
        # Wire the right-aligned view selector into the menu bar.
        # ``setCornerWidget`` with ``Qt.TopRightCorner`` is the
        # Qt-native way to right-align a widget inside a QMenuBar —
        # used widely for search boxes and theme toggles. We do it
        # AFTER ``_init_menu`` so the menubar exists; ``setCornerWidget``
        # queues the widget for display once the layout resolves.
        menubar = self.menuBar()
        if menubar is not None:
            menubar.setCornerWidget(self.view_selector, Qt.TopRightCorner)

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

        # ---- Top-level view tabs (Text / Diagrams / Terminals /
        # ---- Claude) ---------------------------------------------------
        # Post-refactor: the four pages live in a QStackedWidget and
        # are selected by the right-aligned ``view_selector`` combo
        # in the menu bar. We keep the ``_TAB_INDEX_*`` constants so
        # every existing dispatch helper (``_focus_*_tab``,
        # ``_adjust_content_font`` etc.) keeps working unchanged —
        # the combo's index IS the page index.
        self.view_stack = QStackedWidget()

        # Index constants — single source of truth for comparisons.
        # Combo position is the same as the stack index; nothing
        # should hard-code a literal.
        self._TAB_INDEX_DOCUMENTS = 0
        self._TAB_INDEX_DIAGRAMS = 1
        self._TAB_INDEX_TERMINALS = 2
        self._TAB_INDEX_CLAUDE = 3

        # Page 1: Text (formerly "Documents")
        documents_page = QWidget()
        documents_layout = QVBoxLayout(documents_page)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        documents_layout.setSpacing(0)
        documents_layout.addWidget(self.markdown_toolbar)
        documents_layout.addWidget(self.doc_vsplitter)
        self.view_stack.addWidget(documents_page)

        # Page 2: Diagrams
        self.diagram_container = DiagramContainerView()
        self.view_stack.addWidget(self.diagram_container)

        # Page 3: Terminals
        self.terminal_container = TerminalContainerView()
        self.view_stack.addWidget(self.terminal_container)

        # Page 4: Claude
        self.claude_container = ClaudeContainerView()
        self.claude_container.set_proxy_callbacks(
            is_proxy_running=self._is_proxy_running,
            get_proxy_base_url=self._proxy_base_url,
            request_proxy_start=self._request_proxy_start,
        )
        # Default session CWD moved to the global settings store
        # (``Claude > Settings > General``). Wire a loader so
        # ``_launch_session`` can read it without holding its own
        # reference to the global settings file.
        self.claude_container.set_global_settings_loader(
            lambda: self._global_settings_store().load()
        )
        self.claude_container.proxy_requested.connect(self._on_claude_proxy_requested)
        self.view_stack.addWidget(self.claude_container)

        self.setCentralWidget(self.view_stack)

        # Right-aligned view selector. ``currentIndexChanged`` is
        # the cheap equivalent of the old ``view_tabs.currentChanged``
        # (now ``view_selector.currentIndexChanged``) — every dispatch
        # — every dispatch helper that compares against
        # ``_TAB_INDEX_*`` keeps working because combo index = stack
        # index.
        #
        # Created here (after ``view_stack`` exists) rather than
        # lazily in ``_init_menu`` so the ``setCornerWidget`` call
        # below can wire it without a ``QTimer.singleShot``.
        self.view_selector = QComboBox()
        self.view_selector.addItems(["Text", "Diagrams", "Terminals", "Claude"])
        self.view_selector.setCurrentIndex(self._TAB_INDEX_DOCUMENTS)
        self.view_selector.setToolTip(
            "Switch the active top-level view.\n\n"
            "Text — text editor / preview pane (formerly Documents).\n"
            "Diagrams — Mermaid diagram editor.\n"
            "Terminals — local shell sessions.\n"
            "Claude — Claude Code CLI sessions."
        )
        self.view_selector.currentIndexChanged.connect(
            self.view_stack.setCurrentIndex
        )
        self.view_selector.currentIndexChanged.connect(
            self._on_view_changed
        )

    def _on_view_changed(self, index: int) -> None:
        """Sync focus and status bar when the active top-level view changes.

        Renamed from ``_on_view_tab_changed`` after the QTabWidget
        → QStackedWidget + combo refactor. Same semantics — focus
        transfer + status-bar visibility — just driven by the combo
        instead of a tab bar.
        """
        self._update_status_bar_visibility(index)

        if index == getattr(self, "_TAB_INDEX_TERMINALS", -1):
            if self.terminal_container is not None:
                self.terminal_container.focus_active_session()
        if index == getattr(self, "_TAB_INDEX_CLAUDE", -1):
            if self.claude_container is not None:
                self.claude_container.focus_active_session()
        if index == getattr(self, "_TAB_INDEX_DIAGRAMS", -1):
            if self.diagram_container is not None:
                self.diagram_container.focus_active_diagram()

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Stop the proxy thread cleanly before the window closes."""
        thread = self.proxy_thread
        # Stop the clock toolbar's per-second ticker so it doesn't
        # outlive the window. ``stop_timer`` is idempotent; safe to
        # call whether the toolbar exists or not.
        clock_toolbar = getattr(self, "clock_toolbar", None)
        if clock_toolbar is not None and hasattr(clock_toolbar, "stop_timer"):
            clock_toolbar.stop_timer()
        if self.activity_tile is not None:
            self.activity_tile.stop()
        if self.terminal_container is not None:
            self.terminal_container.shutdown()
        if self.claude_container is not None:
            self.claude_container.shutdown()
        if self.diagram_container is not None:
            self.diagram_container.shutdown()
        if thread is not None and thread.isRunning():
            thread.request_stop()
            if not thread.wait(3000):
                thread.terminate()
                thread.wait(1000)
        super().closeEvent(event)
