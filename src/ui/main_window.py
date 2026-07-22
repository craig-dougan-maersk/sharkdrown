"""
Main application window with tabbed interface and split view.

This module provides the main window layout with:
- Tabbed interface for multiple files
- Split view (editor + preview) that can be toggled
- Movable tabs between editor and preview sides
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QAction, QActionGroup, QClipboard, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from src.core.document import DocumentTab
from src.profiles.store import ProfileStore
from src.proxy.activity_tile import ActivityTile
from src.proxy.dashboard_view import DashboardView
from src.proxy.paths import default_db_path
from src.proxy.proxy_config import ProxyConfig
from src.proxy.server_thread import ProxyServerThread
from src.proxy.settings_dialog import ProxySettingsDialog
from src.ui.claude.claude_container_view import ClaudeContainerView
from src.ui.claude.profile_editor_dialog import ProfileEditorDialog
from src.ui.recent_activity_view import RecentActivityView
from src.ui.terminal.terminal_container_view import TerminalContainerView


class MainWindow(QMainWindow):
    """Main application window with tabbed split view."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Pandas Markdown Editor")
        self.resize(1200, 800)

        # State management
        self.split_enabled = True
        self.preview_visible = True
        # Per-instance tab-pair tracking. Was previously declared at class
        # scope, which made every MainWindow share the same dicts.
        self._tab_pairs: dict[int, int] = {}
        self._preview_docs: dict[int, DocumentTab] = {}

        # Embedded proxy state
        self.proxy_thread: ProxyServerThread | None = None
        self.proxy_port: int = 0
        self.proxy_host: str = "127.0.0.1"
        self.dashboard_view: DashboardView | None = None
        self.activity_tile: ActivityTile | None = None
        self.recent_activity_view: RecentActivityView | None = None
        self.terminal_container: TerminalContainerView | None = None
        self.claude_container: ClaudeContainerView | None = None
        self.profile_store: ProfileStore | None = None
        self._proxy_autostart_fired = False
        self._restart_after_stop = False

        # Initialize components
        self._init_central_widget()
        self._init_menu()
        self._init_server_menu()
        self._init_terminal_menu()
        self._init_claude_menu()
        self._init_toolbar()
        self._init_status_bar()

        # Create initial tab
        self._create_new_tab()

        # Honour the user's autostart preference (deferred so the window
        # is fully constructed before we kick off the background thread).
        cfg = ProxyConfig.load()
        if cfg.autostart and not self._proxy_autostart_fired:
            self._proxy_autostart_fired = True
            QTimer.singleShot(0, self._start_server)

    def _init_central_widget(self) -> None:
        """Initialize the central widget.

        Layout: a top-level ``QTabWidget`` with two view modes:

          * **Documents** — the existing editor/preview ``QSplitter``.
            Multiple files can be open as tabs in either pane.
          * **Dashboard** — the embedded proxy dashboard, which fills the
            window when its tab is active. Treated like a document tab
            so it can be opened, closed, and re-opened via the menu.

        Switching to the Dashboard tab "maximises" it (it fills the
        window); switching back to Documents restores the editor.
        """
        # Inner splitter for the editor/preview pair (unchanged).
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        # Tab widget for editor side.
        self.editor_tabs = QTabWidget()
        self.editor_tabs.setTabsClosable(True)
        self.editor_tabs.setMovable(True)
        self.editor_tabs.tabCloseRequested.connect(self._close_tab)
        self.editor_tabs.currentChanged.connect(self._on_editor_tab_changed)

        # Tab widget for preview side.
        self.preview_tabs = QTabWidget()
        self.preview_tabs.setTabsClosable(True)
        self.preview_tabs.setMovable(True)
        self.preview_tabs.tabCloseRequested.connect(self._close_preview_tab)
        self.preview_tabs.currentChanged.connect(self._on_preview_tab_changed)

        self.splitter.addWidget(self.editor_tabs)
        self.splitter.addWidget(self.preview_tabs)

        # ---- Top-level view tabs (Documents / Dashboard) ---------------
        self.view_tabs = QTabWidget()
        self.view_tabs.setDocumentMode(True)
        self.view_tabs.setTabsClosable(False)
        self.view_tabs.setMovable(False)
        # Hold a reference so ``_ensure_dashboard_loaded`` can locate the
        # tab index for the dashboard.
        self._TAB_INDEX_DOCUMENTS = 0
        self._TAB_INDEX_DASHBOARD = 1
        self._TAB_INDEX_RECENT_ACTIVITY = 2
        self._TAB_INDEX_TERMINALS = 3
        self._TAB_INDEX_CLAUDE = 4

        # Page 1: Documents (existing editor/preview splitter).
        documents_page = QWidget()
        documents_layout = QVBoxLayout(documents_page)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        documents_layout.setSpacing(0)
        documents_layout.addWidget(self.splitter)
        self.view_tabs.addTab(documents_page, "Documents")
        self.view_tabs.currentChanged.connect(self._on_view_tab_changed)

        # Page 2: Dashboard placeholder; the actual DashboardView is
        # constructed lazily once the proxy signals it's ready (avoids
        # the heavy QtWebEngine import until needed).
        self.dashboard_container = QWidget()
        self.dashboard_layout = QVBoxLayout(self.dashboard_container)
        self.dashboard_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_layout.setSpacing(0)
        # A placeholder QLabel until the proxy starts; replaced by the
        # real ``DashboardView`` in ``_ensure_dashboard_loaded``.
        self.dashboard_placeholder = QLabel(
            "Dashboard not yet available.\n\n"
            "Start the proxy via:\n"
            "    Server → Start\n\n"
            "Or configure it to auto-start via:\n"
            "    Server → Settings"
        )
        self.dashboard_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dashboard_placeholder.setStyleSheet("color: gray; font-size: 14px;")
        self.dashboard_layout.addWidget(self.dashboard_placeholder)
        self.view_tabs.addTab(self.dashboard_container, "Dashboard")

        # Page 3: Recent Activity — a native Qt table view of the same
        # /api/requests the dashboard uses. Built up-front (no heavy
        # web-engine dep). Polling is disabled until the proxy starts.
        self.recent_activity_view = RecentActivityView()
        self.view_tabs.addTab(self.recent_activity_view, "Recent Activity")

        # Page 4: Terminals — closable tab strip of embedded PTY sessions.
        # Constructed up-front (no heavy deps; just Qt widgets) but no
        # shell is started until the user clicks "+ New Session".
        self.terminal_container = TerminalContainerView()
        self.view_tabs.addTab(self.terminal_container, "Terminals")

        # Page 5: Claude — closable tab strip of Claude Code PTY sessions.
        # Same UX as Terminals; the container takes care of proxy
        # auto-start, env construction, and Claude settings dir layout.
        self.claude_container = ClaudeContainerView()
        self.claude_container.set_proxy_callbacks(
            is_proxy_running=self._is_proxy_running,
            get_proxy_base_url=self._proxy_base_url,
            request_proxy_start=self._request_proxy_start,
        )
        self.claude_container.proxy_requested.connect(self._on_claude_proxy_requested)
        self.view_tabs.addTab(self.claude_container, "Claude")

        self.setCentralWidget(self.view_tabs)

    def _init_menu(self) -> None:
        """Initialize the menu bar."""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")

        self.action_new = QAction("&New", self)
        self.action_new.setShortcut("Ctrl+N")
        # Wrap in a lambda so the action's ``checked`` bool doesn't get
        # passed as ``content`` to ``_create_new_tab`` (it has signature
        # ``(content="", file_path=None)`` and a bool there would silently
        # be coerced into ``doc_tab.content``).
        self.action_new.triggered.connect(lambda _checked=False: self._create_new_tab())
        file_menu.addAction(self.action_new)

        self.action_open = QAction("&Open...", self)
        self.action_open.setShortcut("Ctrl+O")
        self.action_open.triggered.connect(self._open_file)
        file_menu.addAction(self.action_open)

        self.action_save = QAction("&Save", self)
        self.action_save.setShortcut("Ctrl+S")
        self.action_save.triggered.connect(self._save_file)
        file_menu.addAction(self.action_save)

        self.action_save_all = QAction("Save &All", self)
        self.action_save_all.setShortcut("Ctrl+Shift+S")
        self.action_save_all.triggered.connect(self._save_all)
        file_menu.addAction(self.action_save_all)

        file_menu.addSeparator()

        self.action_close = QAction("&Close", self)
        self.action_close.setShortcut("Ctrl+W")
        self.action_close.triggered.connect(self._close_current_tab)
        file_menu.addAction(self.action_close)

        file_menu.addSeparator()

        self.action_exit = QAction("E&xit", self)
        self.action_exit.setShortcut("Ctrl+Q")
        self.action_exit.triggered.connect(self.close)
        file_menu.addAction(self.action_exit)

        # View menu
        view_menu = menubar.addMenu("&View")

        self.action_toggle_split = QAction("&Toggle Split View", self)
        self.action_toggle_split.setShortcut("Ctrl+D")
        self.action_toggle_split.setCheckable(True)
        self.action_toggle_split.setChecked(True)
        self.action_toggle_split.triggered.connect(self._toggle_split)
        view_menu.addAction(self.action_toggle_split)

        self.action_toggle_preview = QAction("&Toggle Preview", self)
        self.action_toggle_preview.setShortcut("Ctrl+P")
        self.action_toggle_preview.setCheckable(True)
        self.action_toggle_preview.setChecked(True)
        self.action_toggle_preview.triggered.connect(self._toggle_preview)
        view_menu.addAction(self.action_toggle_preview)

        view_menu.addSeparator()

        self.action_side_by_side = QAction("Side &By Side", self)
        self.action_side_by_side.setShortcut("Ctrl+1")
        self.action_side_by_side.triggered.connect(lambda: self._set_split_orientation(Qt.Horizontal))
        view_menu.addAction(self.action_side_by_side)

        self.action_stacked = QAction("&Stacked", self)
        self.action_stacked.setShortcut("Ctrl+2")
        self.action_stacked.triggered.connect(lambda: self._set_split_orientation(Qt.Vertical))
        view_menu.addAction(self.action_stacked)

        # Format menu
        format_menu = menubar.addMenu("&Format")

        self.action_format_markdown = QAction("Markdown", self)
        self.action_format_markdown.setCheckable(True)
        self.action_format_markdown.setChecked(True)
        self.action_format_markdown.triggered.connect(lambda: self._set_format("markdown"))
        format_menu.addAction(self.action_format_markdown)

        self.action_format_json = QAction("JSON", self)
        self.action_format_json.setCheckable(True)
        self.action_format_json.triggered.connect(lambda: self._set_format("json"))
        format_menu.addAction(self.action_format_json)

        self.action_format_yaml = QAction("YAML", self)
        self.action_format_yaml.setCheckable(True)
        self.action_format_yaml.triggered.connect(lambda: self._set_format("yaml"))
        format_menu.addAction(self.action_format_yaml)

    def _init_toolbar(self) -> None:
        """Initialize the toolbar."""
        toolbar = QToolBar()
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        # New file
        self.tb_new = QAction(QIcon.fromTheme("document-new"), "New", self)
        # Lambda wrapper — see action_new comment for why.
        self.tb_new.triggered.connect(lambda _checked=False: self._create_new_tab())
        toolbar.addAction(self.tb_new)

        # Open file
        self.tb_open = QAction(QIcon.fromTheme("document-open"), "Open", self)
        self.tb_open.triggered.connect(self._open_file)
        toolbar.addAction(self.tb_open)

        # Save
        self.tb_save = QAction(QIcon.fromTheme("document-save"), "Save", self)
        self.tb_save.triggered.connect(self._save_file)
        toolbar.addAction(self.tb_save)

        toolbar.addSeparator()

        # Toggle split
        self.tb_toggle_split = QAction(QIcon.fromTheme("view-split"), "Toggle Split", self)
        self.tb_toggle_split.setCheckable(True)
        self.tb_toggle_split.setChecked(True)
        self.tb_toggle_split.triggered.connect(self._toggle_split)
        toolbar.addAction(self.tb_toggle_split)

        # Toggle preview
        self.tb_toggle_preview = QAction(QIcon.fromTheme("view-preview"), "Preview", self)
        self.tb_toggle_preview.setCheckable(True)
        self.tb_toggle_preview.setChecked(True)
        self.tb_toggle_preview.triggered.connect(self._toggle_preview)
        toolbar.addAction(self.tb_toggle_preview)

    def _init_status_bar(self) -> None:
        """Initialize the status bar.

        Two permanent widgets sit on the right edge:
          * ``proxy_status_label`` — connection state ("Proxy: running on :3001")
          * ``activity_tile`` — live request counts ("12 req / 2 err / 4 empty")
        The activity tile also serves as a shortcut to the dashboard:
        clicking it focuses the Dashboard tab.
        """
        self.status = self.statusBar()
        self.status.showMessage("Ready")
        self.proxy_status_label = QLabel("Proxy: stopped")
        self.status.addPermanentWidget(self.proxy_status_label)

        self.activity_tile = ActivityTile()
        self.activity_tile.clicked.connect(self._focus_proxy_tab)
        self.status.addPermanentWidget(self.activity_tile)

    def _init_server_menu(self) -> None:
        """Add the Server menu between View and Format.

        Mirrors the conventions used elsewhere in ``_init_menu``: each
        action is stored as ``self.action_*`` so tests can introspect it
        and so the action can be referenced from outside (e.g. the toolbar
        or a status bar menu). State transitions (Start ↔ Stop, etc.) are
        driven by the ``*_on_proxy_*`` slots.
        """
        menubar = self.menuBar()
        server_menu = menubar.addMenu("&Server")

        self.action_start_server = QAction("&Start", self)
        self.action_start_server.triggered.connect(self._start_server)
        server_menu.addAction(self.action_start_server)

        self.action_stop_server = QAction("S&top", self)
        self.action_stop_server.setEnabled(False)
        self.action_stop_server.triggered.connect(self._stop_server)
        server_menu.addAction(self.action_stop_server)

        server_menu.addSeparator()

        self.action_show_dashboard = QAction("Show &Dashboard", self)
        # Checkable so the menu state mirrors which top-level tab is
        # active. ``_on_view_tab_changed`` keeps it in sync.
        self.action_show_dashboard.setCheckable(True)
        self.action_show_dashboard.setChecked(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_dashboard.triggered.connect(self._toggle_dashboard_panel)
        server_menu.addAction(self.action_show_dashboard)

        self.action_show_recent = QAction("Show &Recent Activity", self)
        self.action_show_recent.setEnabled(False)
        self.action_show_recent.triggered.connect(self._show_recent_activity)
        server_menu.addAction(self.action_show_recent)

        self.action_open_browser = QAction("Open in &Browser", self)
        self.action_open_browser.setEnabled(False)
        self.action_open_browser.triggered.connect(self._open_dashboard_external)
        server_menu.addAction(self.action_open_browser)

        server_menu.addSeparator()

        self.action_proxy_settings = QAction("&Settings...", self)
        self.action_proxy_settings.triggered.connect(self._show_proxy_settings)
        server_menu.addAction(self.action_proxy_settings)

    def _init_terminal_menu(self) -> None:
        """Add the Terminal menu between Server and Format.

        Mirrors the conventions of ``_init_server_menu`` — actions stored
        as ``self.action_*`` and always available (no proxy-state gating).

        The Copy / Paste entries use ``QKeySequence.Copy`` /
        ``QKeySequence.Paste`` so the platform-correct shortcuts are
        applied automatically (``Ctrl+C``/``Ctrl+V`` on Linux/Windows,
        ``Cmd+C``/``Cmd+V`` on macOS). They route to the active
        session's text edit via ``_copy_terminal_selection`` and
        ``_paste_to_terminal``, both of which no-op when no session
        is open or the active session has no selection / clipboard text.
        """
        menubar = self.menuBar()
        terminal_menu = menubar.addMenu("&Terminal")

        self.action_new_terminal_session = QAction("&New Terminal Session", self)
        self.action_new_terminal_session.setShortcut("Ctrl+Alt+T")
        self.action_new_terminal_session.triggered.connect(self._new_terminal_session)
        terminal_menu.addAction(self.action_new_terminal_session)

        self.action_focus_terminals = QAction("Focus &Terminals Tab", self)
        self.action_focus_terminals.setShortcut("Ctrl+Alt+Shift+T")
        self.action_focus_terminals.triggered.connect(self._focus_terminals_tab)
        terminal_menu.addAction(self.action_focus_terminals)

        terminal_menu.addSeparator()

        # Edit-style Copy / Paste wired through the menu bar. The
        # text edit's own ``keyPressEvent`` and ``contextMenuEvent``
        # also handle these — the menu items exist so the user can
        # route shortcuts even from non-terminal contexts (e.g. when
        # the Documents tab is focused, ``Ctrl+C`` does text
        # selection, not terminal copy; they have to choose the menu).
        self.action_copy = QAction("&Copy", self)
        self.action_copy.setShortcut(QKeySequence.Copy)
        self.action_copy.triggered.connect(self._copy_terminal_selection)
        terminal_menu.addAction(self.action_copy)

        self.action_paste = QAction("&Paste", self)
        self.action_paste.setShortcut(QKeySequence.Paste)
        self.action_paste.triggered.connect(self._paste_to_terminal)
        terminal_menu.addAction(self.action_paste)

        terminal_menu.addSeparator()

        self.action_select_all = QAction("Select &All", self)
        self.action_select_all.setShortcut(QKeySequence.SelectAll)
        self.action_select_all.triggered.connect(self._select_all_terminal)
        terminal_menu.addAction(self.action_select_all)

        terminal_menu.addSeparator()

        self.action_kill_all_sessions = QAction("&Kill All Sessions", self)
        self.action_kill_all_sessions.triggered.connect(self._kill_all_terminal_sessions)
        terminal_menu.addAction(self.action_kill_all_sessions)

    def _init_claude_menu(self) -> None:
        """Add the Claude menu between Terminal and Format.

        Pattern mirrors ``_init_terminal_menu``: actions stored as
        ``self.action_*``, all always available (no proxy-state
        gating — Claude sessions auto-start the proxy on demand).

        The profile dropdown is a sub-menu populated dynamically from
        :class:`ProfileStore`. ``QActionGroup`` enforces single-selection
        so only one profile can be "active" (drives proxy routing) at
        a time. Switching the active profile triggers a proxy restart
        if one is running — see :meth:`_set_active_profile`.
        """
        menubar = self.menuBar()
        claude_menu = menubar.addMenu("C&laude")

        self.action_new_claude_session = QAction("&New Claude Session", self)
        self.action_new_claude_session.setShortcut("Ctrl+Alt+C")
        self.action_new_claude_session.triggered.connect(self._new_claude_session)
        claude_menu.addAction(self.action_new_claude_session)

        self.action_focus_claude = QAction("Focus &Claude Tab", self)
        self.action_focus_claude.setShortcut("Ctrl+Alt+Shift+C")
        self.action_focus_claude.triggered.connect(self._focus_claude_tab)
        claude_menu.addAction(self.action_focus_claude)

        claude_menu.addSeparator()

        # ---- Profile dropdown -----------------------------------------
        # A QMenu inside the Claude menu; populated by
        # _refresh_profile_menu() on first access and whenever the
        # profile list changes.
        self.profile_menu = QMenu("&Profile", self)
        self.profile_group = QActionGroup(self)
        self.profile_group.setExclusive(True)
        claude_menu.addMenu(self.profile_menu)

        claude_menu.addSeparator()

        self.action_manage_profiles = QAction("&Manage Profiles…", self)
        self.action_manage_profiles.triggered.connect(self._show_profile_editor)
        claude_menu.addAction(self.action_manage_profiles)

        # Populate on first show (lazy — avoids spinning up the
        # ProfileStore during cold start when the user might never
        # open the Claude tab).
        self.profile_menu.aboutToShow.connect(self._refresh_profile_menu)

    def _refresh_profile_menu(self) -> None:
        """Rebuild the profile dropdown from the on-disk store.

        Idempotent — called whenever the menu is about to show and
        after every edit. The checkable actions live inside
        ``self.profile_group`` so exactly one is checked (the active
        profile); switching another triggers ``_set_active_profile``.
        """
        # Clear existing actions; tear them out of the group too so
        # they don't get GC'd while still referenced.
        for action in list(self.profile_group.actions()):
            self.profile_group.removeAction(action)
        self.profile_menu.clear()

        store = self._profile_store()
        profiles = store.load()
        active_name = store.get_active_name() or (
            profiles[0].name if profiles else None
        )

        for p in profiles:
            act = QAction(p.name, self)
            act.setCheckable(True)
            act.setChecked(p.name == active_name)
            act.triggered.connect(lambda _checked=False, n=p.name: self._set_active_profile(n))
            self.profile_group.addAction(act)
            self.profile_menu.addAction(act)

        if not profiles:
            placeholder = QAction("(no profiles)", self)
            placeholder.setEnabled(False)
            self.profile_menu.addAction(placeholder)

    def _create_new_tab(self, content: str = "", file_path: Path | None = None) -> None:
        """Create a new editor tab with optional content."""
        # Create the document tab
        doc_tab = DocumentTab(file_path, content)

        # Create editor widget
        from src.ui.widgets.editor_widget import EditorWidget
        editor = EditorWidget()
        editor.document = doc_tab

        # Add to editor tabs
        index = self.editor_tabs.addTab(editor, doc_tab.title())
        self.editor_tabs.setCurrentIndex(index)

        # Update tab title when content changes
        editor.text_changed.connect(lambda: self._update_tab_title(index, doc_tab))
        # Re-render the paired preview on every edit. The editor already
        # writes the new text onto doc_tab.content; this just pushes it to
        # whichever preview (if any) is paired with this editor tab.
        editor.content_changed.connect(
            lambda text: self._on_editor_content_changed(index, doc_tab)
        )

        # Create a paired preview tab if the view state allows it
        if self.split_enabled and self.preview_visible:
            self._create_preview_for(index, doc_tab)

    def _create_preview_for(self, editor_index: int, doc_tab: DocumentTab) -> None:
        """Create a preview tab paired to the given editor tab."""
        from src.ui.widgets.preview_widget import PreviewWidget

        preview = PreviewWidget()
        preview.set_document(doc_tab)

        pindex = self.preview_tabs.addTab(preview, f"Preview: {doc_tab.title()}")
        self.preview_tabs.setCurrentIndex(pindex)

        # Track the relationship between editor and preview tabs
        self._tab_pairs[editor_index] = pindex
        self._preview_docs[pindex] = doc_tab

    def _update_tab_title(self, index: int, doc_tab: DocumentTab) -> None:
        """Update tab title when file is saved or renamed."""
        self.editor_tabs.setTabText(index, doc_tab.title())
        if index in self._tab_pairs:
            pindex = self._tab_pairs[index]
            self.preview_tabs.setTabText(pindex, f"Preview: {doc_tab.title()}")

    def _on_editor_content_changed(
        self, editor_index: int, doc_tab: DocumentTab
    ) -> None:
        """Re-render the preview paired with ``editor_index``, if any.

        doc_tab.content is already up-to-date (EditorWidget writes it
        before emitting content_changed). If no preview tab is currently
        paired — e.g. the user turned the preview off — this is a no-op;
        the next time a preview is created, set_document will render the
        current content.
        """
        if editor_index not in self._tab_pairs:
            return
        pindex = self._tab_pairs[editor_index]
        preview = self.preview_tabs.widget(pindex)
        if preview is not None:
            preview._render_preview()

    def _open_file(self) -> None:
        """Open a file dialog and load the selected file."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open File",
            str(Path.home()),
            "All Supported Files (*.md *.markdown *.json *.yaml *.yml);;Markdown Files (*.md *.markdown);;JSON Files (*.json);;YAML Files (*.yaml *.yml)",
        )
        if file_path:
            self._load_file(Path(file_path))

    def _load_file(self, file_path: Path) -> None:
        """Load a file into a new tab."""
        try:
            content = file_path.read_text(encoding="utf-8")
            self._create_new_tab(content, file_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open file: {e}")

    def _save_file(self) -> None:
        """Save the current editor tab to file."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return

        editor = self.editor_tabs.widget(current_idx)
        doc_tab = editor.document

        if doc_tab.file_path is None:
            # Ask for save location
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save File",
                str(Path.home() / "untitled.md"),
                "Markdown Files (*.md *.markdown);;JSON Files (*.json);;YAML Files (*.yaml *.yml);;All Files (*)",
            )
            if file_path:
                doc_tab.file_path = Path(file_path)
        else:
            file_path = doc_tab.file_path

        try:
            file_path.write_text(editor.toPlainText(), encoding="utf-8")
            doc_tab.content = editor.toPlainText()
            self.status.showMessage(f"Saved: {file_path.name}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save file: {e}")

    def _save_all(self) -> None:
        """Save all editor tabs."""
        for i in range(self.editor_tabs.count()):
            editor = self.editor_tabs.widget(i)
            doc_tab = editor.document
            if doc_tab.file_path:
                try:
                    doc_tab.file_path.write_text(editor.toPlainText(), encoding="utf-8")
                    doc_tab.content = editor.toPlainText()
                except Exception as e:
                    QMessageBox.warning(self, "Warning", f"Failed to save {doc_tab.title()}: {e}")
        self.status.showMessage("All files saved", 3000)

    def _close_tab(self, index: int) -> None:
        """Close an editor tab."""
        # Close corresponding preview tab if it exists
        if index in self._tab_pairs:
            pindex = self._tab_pairs[index]
            self.preview_tabs.removeTab(pindex)
            del self._tab_pairs[index]

        self.editor_tabs.removeTab(index)

    def _close_preview_tab(self, index: int) -> None:
        """Close a preview tab."""
        # Find and close corresponding editor tab
        for e_idx, p_idx in list(self._tab_pairs.items()):
            if p_idx == index:
                self.editor_tabs.removeTab(e_idx)
                del self._tab_pairs[e_idx]
                break

    def _close_current_tab(self) -> None:
        """Close the current tab."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx >= 0:
            self._close_tab(current_idx)

    def _on_editor_tab_changed(self, index: int) -> None:
        """Handle editor tab change."""
        if index in self._tab_pairs:
            self.preview_tabs.setCurrentIndex(self._tab_pairs[index])

    def _on_preview_tab_changed(self, index: int) -> None:
        """Handle preview tab change."""
        # Keep editor in sync
        pass

    def _apply_view_state(self) -> None:
        """Apply the current split/preview visibility state to the UI."""
        show_preview = self.split_enabled and self.preview_visible
        self.preview_tabs.setVisible(show_preview)

        if show_preview:
            # Sync the visible preview tab to the active editor tab
            current = self.editor_tabs.currentIndex()
            if current in self._tab_pairs:
                self.preview_tabs.setCurrentIndex(self._tab_pairs[current])

    def _toggle_split(self) -> None:
        """Toggle the split view on/off.

        Splitting only shows/hides the second (preview) panel. The preview
        preference itself is independent, so toggling split back on restores
        the preview if it was enabled.
        """
        self.split_enabled = not self.split_enabled
        self.action_toggle_split.setChecked(self.split_enabled)
        self.tb_toggle_split.setChecked(self.split_enabled)
        self._apply_view_state()

    def _toggle_preview(self) -> None:
        """Toggle the preview visibility."""
        self.preview_visible = not self.preview_visible

        if self.preview_visible:
            # Preview lives inside the split, so the split must be on
            self.split_enabled = True
            self.action_toggle_split.setChecked(True)
            self.tb_toggle_split.setChecked(True)

            # Ensure the current editor has a preview tab
            current = self.editor_tabs.currentIndex()
            if current >= 0 and current not in self._tab_pairs:
                editor = self.editor_tabs.widget(current)
                self._create_preview_for(current, editor.document)

        self.action_toggle_preview.setChecked(self.preview_visible)
        self.tb_toggle_preview.setChecked(self.preview_visible)
        self._apply_view_state()

    def _set_split_orientation(self, orientation: Qt.Orientation) -> None:
        """Set the split orientation (horizontal or vertical)."""
        self.splitter.setOrientation(orientation)

    def _set_format(self, format_type: str) -> None:
        """Set the current document format."""
        current_idx = self.editor_tabs.currentIndex()
        if current_idx >= 0:
            editor = self.editor_tabs.widget(current_idx)
            editor.document.format_type = format_type

    # ------------------------------------------------------------------
    # Embedded proxy control
    # ------------------------------------------------------------------
    def _start_server(self) -> None:
        """Start (or restart) the embedded proxy on its own QThread.

        Safe to call when the thread is already running — the early return
        prevents a double-start. The thread reads its configuration from
        QSettings so the user can change port/host via the Settings dialog
        without editing this code.
        """
        if self.proxy_thread is not None and self.proxy_thread.isRunning():
            return

        cfg = ProxyConfig.load()
        self.proxy_host = cfg.host
        self.proxy_port = cfg.port

        self.proxy_status_label.setText(f"Proxy: starting on {cfg.base_url}…")
        self.action_start_server.setEnabled(False)
        self.action_stop_server.setEnabled(True)

        thread = ProxyServerThread(
            host=cfg.host,
            port=cfg.port,
            storage_path=default_db_path(),
            config_dir=cfg.config_dir_path,
            model_endpoints=cfg.model_endpoints,
            default_endpoint=cfg.default_endpoint or None,
            parent=self,
        )
        thread.started_ok.connect(self._on_proxy_started)
        thread.failed.connect(self._on_proxy_failed)
        thread.stopped.connect(self._on_proxy_stopped)
        self.proxy_thread = thread
        thread.start()

    def _stop_server(self) -> None:
        """Ask the proxy thread to shut down (non-blocking).

        uvicorn checks ``should_exit`` between requests and unwinds the
        lifespan's ``finally`` block, which closes SQLite and AsyncClients.
        The ``stopped`` signal flips the UI back to "stopped" state.
        """
        if self.proxy_thread is None:
            return
        self.proxy_status_label.setText("Proxy: stopping…")
        self.action_stop_server.setEnabled(False)
        self.proxy_thread.request_stop()

    def _on_proxy_started(self, port: int) -> None:
        """Called by the worker thread once uvicorn is listening."""
        # ``port`` is the actual bound port — equals the requested one
        # unless we ever expose port=0 in the settings dialog.
        self.proxy_port = port
        self.proxy_status_label.setText(f"Proxy: running on {self.proxy_host}:{port}")
        self.action_start_server.setEnabled(True)
        self.action_stop_server.setEnabled(True)
        self.action_show_dashboard.setEnabled(True)
        self.action_show_recent.setEnabled(True)
        self.action_open_browser.setEnabled(True)
        # Kick off the activity tile's polling against the live proxy.
        if self.activity_tile is not None:
            self.activity_tile.set_base_url(f"http://{self.proxy_host}:{port}")
        # Same for the Recent Activity tab.
        if self.recent_activity_view is not None:
            self.recent_activity_view.set_base_url(f"http://{self.proxy_host}:{port}")
        self.status.showMessage(
            f"Proxy ready — point Claude Code at http://{self.proxy_host}:{port}",
            5000,
        )
        # Lazily construct the dashboard view if the user hasn't opened
        # the tab yet. This avoids the heavy QWebEngineWidgets import
        # until the server is actually running.
        self._ensure_dashboard_loaded()
        # Flush any Claude session that was queued while the proxy
        # was starting. The container holds at most one pending
        # launch; if nothing was queued this is a no-op.
        if self.claude_container is not None:
            self.claude_container.flush_pending_launch()

    def _on_proxy_failed(self, msg: str) -> None:
        """Called when the thread or uvicorn fails to start."""
        self.proxy_status_label.setText("Proxy: failed")
        self.action_start_server.setEnabled(True)
        self.action_stop_server.setEnabled(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_recent.setEnabled(False)
        self.action_open_browser.setEnabled(False)
        QMessageBox.critical(self, "Proxy Error", msg)

    def _on_proxy_stopped(self) -> None:
        """Called after the lifespan has fully torn down."""
        self.proxy_status_label.setText("Proxy: stopped")
        self.action_start_server.setEnabled(True)
        self.action_stop_server.setEnabled(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_recent.setEnabled(False)
        self.action_open_browser.setEnabled(False)
        self.action_show_dashboard.setChecked(False)
        # Stop polling — the URL is now invalid.
        if self.activity_tile is not None:
            self.activity_tile.stop()
        if self.recent_activity_view is not None:
            self.recent_activity_view.stop()
        # Drop our reference to the finished QThread. Qt's event loop
        # will then clean up the underlying C++ object on the next pass.
        # Without this, closeEvent later would touch a dangling object.
        self.proxy_thread = None
        # If the user just edited settings while the proxy was running,
        # restart it now with the new configuration.
        if getattr(self, "_restart_after_stop", False):
            self._restart_after_stop = False
            QTimer.singleShot(0, self._start_server)

    def _ensure_dashboard_loaded(self) -> None:
        """Lazy-build the DashboardView inside the Dashboard tab.

        Called only when the proxy has actually started — this avoids
        loading the heavy QtWebEngine module during cold-start. The
        placeholder shown in the Dashboard tab stays put until this
        runs. Subsequent calls just reload the URL with the new port.
        """
        if self.proxy_port == 0:
            # Proxy hasn't started yet; leave the placeholder visible.
            return
        if self.dashboard_view is None:
            self.dashboard_view = DashboardView()
            self.dashboard_view.url_loaded.connect(
                lambda url: self.status.showMessage(f"Dashboard loaded: {url}", 3000)
            )
            # Swap the placeholder out for the actual web view. Use
            # deleteLater() so any in-flight QResizeEvent from the
            # placeholder doesn't fight with the new widget.
            old = self.dashboard_placeholder
            self.dashboard_placeholder = self.dashboard_view  # type: ignore[assignment]
            self.dashboard_layout.replaceWidget(old, self.dashboard_view)
            old.deleteLater()

        # (Re)point the WebView at the proxy URL — works for both first
        # use and after a stop/start cycle.
        self.dashboard_view.set_base_url(f"http://{self.proxy_host}:{self.proxy_port}")

    def _focus_proxy_tab(self) -> None:
        """Bring the proxy's primary tab to the front.

        Called by the activity tile in the status bar. Prefers the
        Recent Activity tab if it exists (it's a slimmer, more focused
        view than the full dashboard); falls back to Dashboard when
        Recent Activity is unavailable. Documents is the last resort
        so the click still does something visible if proxy state is
        stale.
        """
        # The attribute only exists once Task #19 ships.
        recent_idx = getattr(self, "_TAB_INDEX_RECENT_ACTIVITY", None)
        dashboard_idx = getattr(
            self, "_TAB_INDEX_DASHBOARD", self._TAB_INDEX_DASHBOARD
        )
        if recent_idx is not None and self.view_tabs.widget(recent_idx) is not None:
            self.view_tabs.setCurrentIndex(recent_idx)
            return
        self.view_tabs.setCurrentIndex(dashboard_idx)

    def _show_recent_activity(self) -> None:
        """Slot for Server → Show Recent Activity."""
        idx = getattr(self, "_TAB_INDEX_RECENT_ACTIVITY", None)
        if idx is not None:
            self.view_tabs.setCurrentIndex(idx)

    def _toggle_dashboard_panel(self, visible: bool) -> None:
        """Slot for the Server → Show Dashboard action.

        Switches the top-level view tabs to the Dashboard page. The
        actual ``DashboardView`` is built by ``_on_proxy_started`` when
        the proxy signals it's listening; this just switches the tab.
        """
        if visible:
            self.view_tabs.setCurrentIndex(self._TAB_INDEX_DASHBOARD)
        else:
            self.view_tabs.setCurrentIndex(self._TAB_INDEX_DOCUMENTS)

    def _on_view_tab_changed(self, index: int) -> None:
        """Sync the Server → Show Dashboard action with the active tab.

        Blocks signals on the action so clicking the menu doesn't
        recursively drive the tab bar. Also transfers keyboard focus to
        the visible widget — without this, the QTabBar keeps focus after
        a click on a tab title and subsequent keystrokes go to it
        instead of the visible widget (this is the root cause of
        "typing in the terminal does nothing" when the user clicks the
        Terminals tab).
        """
        is_dashboard = index == self._TAB_INDEX_DASHBOARD
        self.action_show_dashboard.blockSignals(True)
        self.action_show_dashboard.setChecked(is_dashboard)
        self.action_show_dashboard.blockSignals(False)

        # If the Terminals tab just became active, focus the currently
        # selected session's text edit. ``focus_active_session`` is a
        # no-op when there are no sessions yet.
        if index == getattr(self, "_TAB_INDEX_TERMINALS", -1):
            if self.terminal_container is not None:
                self.terminal_container.focus_active_session()
        # Same for Claude — without the focus transfer, keystrokes
        # after switching tabs would go to the QTabBar (Alt+arrows
        # become tab-navigation shortcuts, etc.).
        if index == getattr(self, "_TAB_INDEX_CLAUDE", -1):
            if self.claude_container is not None:
                self.claude_container.focus_active_session()

    def _open_dashboard_external(self) -> None:
        """Open the dashboard in the user's default web browser."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = f"http://{self.proxy_host}:{self.proxy_port}/"
        QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------
    # Terminal session control
    # ------------------------------------------------------------------
    def _new_terminal_session(self) -> None:
        """Open a new shell session in the Terminals tab and switch to it."""
        if self.terminal_container is None:
            return
        self.terminal_container.new_session()
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_TERMINALS)

    def _focus_terminals_tab(self) -> None:
        """Slot for Terminal → Focus Terminals Tab (Ctrl+Alt+Shift+T)."""
        idx = getattr(self, "_TAB_INDEX_TERMINALS", None)
        if idx is not None:
            self.view_tabs.setCurrentIndex(idx)

    def _active_terminal_session(self):
        """Return the active ``TerminalSessionView`` or ``None``.

        Used by the menu Copy / Paste / Select All slots to route to
        whichever session is currently displayed in the inner
        ``session_tabs``. Returns ``None`` when the container hasn't
        been built or there are no sessions.
        """
        if self.terminal_container is None:
            return None
        sess = self.terminal_container.session_tabs.currentWidget()
        # Imported lazily to avoid the Qt-coupled import at module load.
        from src.ui.terminal.terminal_session_view import TerminalSessionView
        if isinstance(sess, TerminalSessionView):
            return sess
        return None

    def _copy_terminal_selection(self) -> None:
        """Menu-bar hook for Terminal → Copy.

        Reads from the active session's text edit. No-op when no
        session is active or the selection is empty.
        """
        sess = self._active_terminal_session()
        if sess is None:
            return
        sess.text_edit._copy_selection()

    def _paste_to_terminal(self) -> None:
        """Menu-bar hook for Terminal → Paste.

        Reads the system clipboard and forwards to the active session.
        No-op when no session is active.
        """
        sess = self._active_terminal_session()
        if sess is None:
            return
        sess.text_edit._paste_from(mode=QClipboard.Clipboard)

    def _select_all_terminal(self) -> None:
        """Menu-bar hook for Terminal → Select All.

        Selects every cell of the active session's screen. No-op when
        no session is active.
        """
        sess = self._active_terminal_session()
        if sess is None:
            return
        sess.text_edit.select_all()

    def _kill_all_terminal_sessions(self) -> None:
        """Slot for Terminal → Kill All Sessions.

        Uses QMessageBox to confirm — killing is destructive and removes
        all in-flight work in the sessions.
        """
        if self.terminal_container is None or self.terminal_container.session_count == 0:
            self.status.showMessage("No terminal sessions to kill.", 3000)
            return
        reply = QMessageBox.question(
            self,
            "Kill All Sessions",
            f"Kill all {self.terminal_container.session_count} terminal session(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.terminal_container.shutdown()
        self.status.showMessage("Killed all terminal sessions.", 3000)

    def _show_proxy_settings(self) -> None:
        """Open the settings dialog; restart the proxy if the user changed settings."""
        was_running = self.proxy_thread is not None and self.proxy_thread.isRunning()
        current = ProxyConfig.load()
        dialog = ProxySettingsDialog(current, parent=self)
        new_cfg, changed = dialog.exec_and_return()
        if not changed:
            return
        new_cfg.save()
        if was_running:
            self.status.showMessage("Restarting proxy with new settings...", 3000)
            self._stop_server()
            # Stop is async; wait briefly then start the new instance.
            # The thread emits stopped() which we already handle; we just
            # need to know when it's safe to call _start_server again.
            self._restart_after_stop = True
        else:
            self.status.showMessage("Proxy settings saved.", 3000)

    # ------------------------------------------------------------------
    # Claude session control
    # ------------------------------------------------------------------

    def _profile_store(self) -> ProfileStore:
        """Lazy-init the :class:`ProfileStore` (requires a QApplication)."""
        if self.profile_store is None:
            self.profile_store = ProfileStore(
                path=default_db_path().parent / "profiles.json",
            )
        return self.profile_store

    def _is_proxy_running(self) -> bool:
        return self.proxy_thread is not None and self.proxy_thread.isRunning()

    def _proxy_base_url(self) -> str:
        """URL Claude Code sessions should use. Empty if proxy isn't up."""
        if not self._is_proxy_running() or self.proxy_port == 0:
            return ""
        return f"http://{self.proxy_host}:{self.proxy_port}"

    def _request_proxy_start(self) -> None:
        """Start the proxy if not already running.

        Called from :class:`ClaudeContainerView` when a new session
        is requested before the proxy is up. Returns immediately —
        the container queues its launch and picks it up when the
        proxy's ``started_ok`` signal fires.
        """
        if not self._is_proxy_running():
            self._start_server()

    def _on_claude_proxy_requested(self) -> None:
        """Slot for :attr:`ClaudeContainerView.proxy_requested`.

        Just calls :meth:`_request_proxy_start` — kept separate so
        the signal-to-slot wiring has a clear name in the constructor.
        """
        self._request_proxy_start()

    def _new_claude_session(self) -> None:
        """Open a new Claude session in the Claude tab and switch to it."""
        if self.claude_container is None:
            return
        # Use the active profile as the default. If the user wants a
        # different profile for this specific session, we offer a picker.
        profiles = self._profile_store().load()
        if not profiles:
            QMessageBox.information(
                self,
                "No profiles",
                "No Claude profiles configured. Open "
                "Claude → Manage Profiles… to create one.",
            )
            return
        active = self._profile_store().get_active()
        profile = active
        if len(profiles) > 1:
            names = [p.name for p in profiles]
            current = names.index(active.name) if active else 0
            choice, ok = QInputDialog.getItem(
                self,
                "Profile for new session",
                "Use which profile?",
                names,
                current,
                False,
            )
            if not ok:
                return
            profile = next(p for p in profiles if p.name == choice)
        view = self.claude_container.new_session(profile=profile)
        # Switch to the Claude tab so the user sees the new session.
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
        # If the launch was queued (proxy not yet up), ``view`` is
        # None — the proxy start signal will eventually launch it.

    def _focus_claude_tab(self) -> None:
        """Slot for Claude → Focus Claude Tab."""
        idx = getattr(self, "_TAB_INDEX_CLAUDE", None)
        if idx is not None:
            self.view_tabs.setCurrentIndex(idx)

    def _set_active_profile(self, name: str) -> None:
        """Switch the active profile; restart the proxy if running.

        Per the user's design decision: profile switching triggers a
        proxy restart (~1-2 s downtime, drops in-flight requests).
        We show a status-bar message so the user knows what's
        happening.
        """
        store = self._profile_store()
        try:
            store.set_active(name)
        except KeyError:
            return
        # Update the proxy's model_endpoints to match the new profile.
        # If the proxy is running we have to bounce it; if not, the
        # next ``_start_server`` reads the active profile's endpoints.
        if self._is_proxy_running():
            self.status.showMessage(
                f"Restarting proxy to apply profile {name!r}…",
                3000,
            )
            self._restart_after_stop = True
            self._stop_server()
        else:
            self._refresh_profile_menu()

    def _show_profile_editor(self) -> None:
        """Open the Manage Profiles dialog.

        The dialog edits a single profile at a time. To avoid
        bouncing between dialogs, we use a small "list + edit"
        pattern: an initial dialog (or a custom sub-dialog)
        presents a list of profiles plus New / Edit / Delete
        actions. For v1, we open the editor for the active
        profile and let the user edit the rest via the dropdown
        on the way out.

        A more complete multi-profile manager is Phase 4; for now
        this gives the user a working path to edit / create
        profiles without disrupting the menu.
        """
        store = self._profile_store()
        profiles = store.load()
        if not profiles:
            # No profiles — open editor for a fresh "New Profile"
            # draft and let the user fill it in.
            from src.profiles.models import ClaudeProfile, slugify

            draft = ClaudeProfile(
                name="",
                slug=slugify(""),
                model_endpoints={},
            )
        else:
            # Pick which profile to edit. If only one exists, jump
            # straight into editing it.
            if len(profiles) == 1:
                draft = profiles[0]
            else:
                names = [p.name for p in profiles]
                active = store.get_active()
                current = (
                    names.index(active.name) if active in profiles else 0
                )
                choice, ok = QInputDialog.getItem(
                    self,
                    "Manage Profiles",
                    "Edit which profile?",
                    names,
                    current,
                    False,
                )
                if not ok:
                    return
                draft = next(p for p in profiles if p.name == choice)

        dlg = ProfileEditorDialog(
            draft, credential_store=store.credential_store, parent=self
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        edited = dlg.to_profile()
        if not edited.name:
            return
        store.upsert(edited)
        self.status.showMessage(f"Saved profile {edited.name!r}.", 3000)
        self._refresh_profile_menu()
        # If the proxy is running and we just changed the active
        # profile's endpoints, the model_endpoints may have drifted —
        # restart to pick them up.
        active_name = store.get_active_name()
        if self._is_proxy_running() and active_name == edited.name:
            self._restart_after_stop = True
            self._stop_server()
        elif not self._is_proxy_running():
            # No proxy running — nothing to restart, but the next
            # session launch will read the updated endpoints.
            pass

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Stop the proxy thread cleanly before the window closes."""
        thread = self.proxy_thread
        # Stop the activity tile's polling before tearing down the proxy
        # — avoids one final httpx call after the port has been released.
        if self.activity_tile is not None:
            self.activity_tile.stop()
        if self.recent_activity_view is not None:
            self.recent_activity_view.shutdown()
        if self.terminal_container is not None:
            # Shut down all PTY sessions BEFORE the proxy thread so that
            # any child shells that talk to the proxy get a chance to
            # disconnect cleanly. Container.shutdown() is idempotent and
            # bounded — same philosophy as the proxy thread teardown.
            self.terminal_container.shutdown()
        if self.claude_container is not None:
            # Same pattern — Claude Code sessions are PTY children too,
            # and they hold connections to the proxy. Shutting them
            # down before the proxy thread tears down avoids stray
            # asyncio tasks referencing a closed event loop.
            self.claude_container.shutdown()
        if thread is not None and thread.isRunning():
            thread.request_stop()
            # Give uvicorn up to 3s to drain; force-terminate otherwise.
            if not thread.wait(3000):
                thread.terminate()
                thread.wait(1000)
        super().closeEvent(event)