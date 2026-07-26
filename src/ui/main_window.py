"""
Main application window with tabbed interface and split view.

This module provides the main window layout with:
- Tabbed interface for multiple files
- Split view (editor + preview) that can be toggled
- Movable tabs between editor and preview sides
"""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QClipboard, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.config import APP_TITLE
from src.core.document import DocumentTab
from src.profiles.store import ProfileStore
from src.proxy.activity_tile import ActivityTile
from src.proxy.paths import default_db_path
from src.proxy.proxy_config import ProxyConfig
from src.proxy.server_thread import ProxyServerThread
from src.ui.claude.claude_container_view import ClaudeContainerView
from src.ui.claude.profile_editor_dialog import ProfileEditorDialog
from src.ui.terminal.terminal_container_view import TerminalContainerView


class MainWindow(QMainWindow):
    """Main application window with tabbed split view."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1200, 800)

        # State management
        # ``split_enabled`` and ``preview_visible`` default to False:
        # the user wants a single-pane editor on startup. Toggling
        # Preview re-enables Split (the existing escalation logic in
        # ``_toggle_preview``), and toggling Split reveals the empty
        # second pane without surfacing the preview.
        self.split_enabled = False
        self.preview_visible = False
        # Per-instance tab-pair tracking. Was previously declared at class
        # scope, which made every MainWindow share the same dicts.
        self._tab_pairs: dict[int, int] = {}
        self._preview_docs: dict[int, DocumentTab] = {}

        # Embedded proxy state
        self.proxy_thread: ProxyServerThread | None = None
        self.proxy_port: int = 0
        self.proxy_host: str = "127.0.0.1"
        self.activity_tile: ActivityTile | None = None
        self.terminal_container: TerminalContainerView | None = None
        self.claude_container: ClaudeContainerView | None = None
        self.profile_store: ProfileStore | None = None
        self._proxy_autostart_fired = False
        self._restart_after_stop = False

        # Initialize components
        self._init_central_widget()
        self._init_menu()
        self._init_terminal_menu()
        self._init_claude_menu()
        self._init_status_bar()

        # Create initial tab
        self._create_new_tab()

        # Apply the initial split/preview visibility. The splitter and
        # preview_tabs are created in their default-visible state by
        # _init_central_widget, so without this call the window would
        # open as a 2-pane layout regardless of the split_enabled
        # default. Call after _create_new_tab so the existing
        # tab-pair logic is in place.
        self._apply_view_state()

        # Sync the status-bar format combo to the first document's
        # detected format, and gate the right-edge widgets for the
        # current top-level tab. The first call to ``_on_view_tab_changed``
        # happens on user click, so the initial state must be set
        # explicitly here.
        self._sync_format_combo_to_current_document()
        self._update_status_bar_visibility(self._TAB_INDEX_DOCUMENTS)

        # Honour the user's autostart preference (deferred so the window
        # is fully constructed before we kick off the background thread).
        cfg = ProxyConfig.load()
        if cfg.autostart and not self._proxy_autostart_fired:
            self._proxy_autostart_fired = True
            QTimer.singleShot(0, self._start_server)

    def _init_central_widget(self) -> None:
        """Initialize the central widget.

        Layout: a top-level ``QTabWidget`` with three top-level tabs:

          * **Documents** — the existing editor/preview ``QSplitter``.
            Multiple files can be open as tabs in either pane.
          * **Terminals** — closable tab strip of embedded PTY sessions.
          * **Claude** — owns a nested tab strip with the proxy
            Dashboard (pinned), Recent Activity (pinned), and any
            number of Claude Code sessions (closable). See
            :class:`ClaudeContainerView` for the inner structure.

        Switching to the Claude tab shows the inner tab strip; the
        inner selection tracks the user's last activity in the
        container (default: Dashboard).
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

        # ---- Top-level view tabs (Documents / Terminals / Claude) ------
        self.view_tabs = QTabWidget()
        self.view_tabs.setDocumentMode(True)
        self.view_tabs.setTabsClosable(False)
        self.view_tabs.setMovable(False)
        # Tab indices — kept as constants so menu slots and tests can
        # refer to them without re-counting. The Claude container
        # owns its own internal pinned/session tab strip; these
        # constants only refer to the top-level view tabs.
        self._TAB_INDEX_DOCUMENTS = 0
        self._TAB_INDEX_TERMINALS = 1
        self._TAB_INDEX_CLAUDE = 2

        # Page 1: Documents (existing editor/preview splitter).
        documents_page = QWidget()
        documents_layout = QVBoxLayout(documents_page)
        documents_layout.setContentsMargins(0, 0, 0, 0)
        documents_layout.setSpacing(0)
        documents_layout.addWidget(self.splitter)
        self.view_tabs.addTab(documents_page, "Documents")
        self.view_tabs.currentChanged.connect(self._on_view_tab_changed)

        # Page 2: Terminals — closable tab strip of embedded PTY sessions.
        # Constructed up-front (no heavy deps; just Qt widgets) but no
        # shell is started until the user clicks "+ New Session".
        self.terminal_container = TerminalContainerView()
        self.view_tabs.addTab(self.terminal_container, "Terminals")

        # Page 3: Claude — owns the proxy Dashboard, Recent Activity,
        # and a closable tab strip of Claude Code sessions. The
        # container takes care of proxy auto-start, env construction,
        # and Claude settings dir layout.
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
        self.action_toggle_split.setChecked(False)
        self.action_toggle_split.triggered.connect(self._toggle_split)
        view_menu.addAction(self.action_toggle_split)

        self.action_toggle_preview = QAction("&Toggle Preview", self)
        self.action_toggle_preview.setShortcut("Ctrl+P")
        self.action_toggle_preview.setCheckable(True)
        self.action_toggle_preview.setChecked(False)
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

        # Format menu was removed — the format is now picked from a
        # drop-down selector in the status bar (see ``_init_status_bar``),
        # visible only on the Documents tab.

    def _init_status_bar(self) -> None:
        """Initialize the status bar.

        Three permanent widgets live on the right edge, but their
        visibility is gated by the active top-level tab:

        * ``format_combo`` — visible only on the **Documents** tab.
          Picks the current document's format (markdown / json / yaml).
          Synced to the active editor tab on tab switches via
          ``_on_editor_tab_changed``.
        * ``proxy_status_label`` — visible only on the **Claude** tab.
          Connection state ("Proxy: running on :3001").
        * ``activity_tile`` — visible only on the **Claude** tab. Live
          request counts ("12 req / 2 err / 4 empty"); also serves as a
          shortcut to the Claude container's Dashboard sub-tab.

        On Terminals, the right edge of the status bar is empty (the
        transient ``showMessage`` text on the left still appears).
        """
        self.status = self.statusBar()
        self.status.showMessage("Ready")

        # ---- Format selector -------------------------------------------
        # The user picks the current document's format from this combo.
        # ``Markdown / JSON / YAML`` strings match the values used by
        # ``DocumentTab.format_type`` and the ``EditorWidget`` highlighter
        # switch in ``_update_highlighter``.
        self.format_combo = QComboBox()
        self.format_combo.addItems(["markdown", "json", "yaml"])
        self.format_combo.setToolTip(
            "Format for the current document.\n"
            "Drives syntax highlighting and preview rendering."
        )
        # ``currentTextChanged`` fires on every change, including
        # programmatic ones — the sync helpers block signals during
        # assignment so we don't bounce.
        self.format_combo.currentTextChanged.connect(self._on_format_combo_changed)
        self.status.addPermanentWidget(self.format_combo)

        # ---- Proxy status (Claude tab only) ---------------------------
        self.proxy_status_label = QLabel("Proxy: stopped")
        self.status.addPermanentWidget(self.proxy_status_label)

        # ---- Activity tile (Claude tab only) --------------------------
        self.activity_tile = ActivityTile()
        self.activity_tile.clicked.connect(self._focus_proxy_tab)
        self.status.addPermanentWidget(self.activity_tile)

        # Default visibility: Documents tab is the initial tab, so
        # the format combo is on, the proxy widgets are off. The actual
        # gate is ``_update_status_bar_visibility`` which runs in
        # ``_on_view_tab_changed``.
        self.proxy_status_label.setVisible(False)
        self.activity_tile.setVisible(False)

    # ------------------------------------------------------------------
    # Status-bar tab-conditional visibility
    # ------------------------------------------------------------------

    def _update_status_bar_visibility(self, view_index: int) -> None:
        """Show the right permanent widgets for the active top-level tab.

        * **Documents** — ``format_combo`` visible; proxy widgets hidden.
        * **Terminals** — all right-edge widgets hidden (the message
          area on the left is unaffected).
        * **Claude** — proxy status + activity tile visible; format
          combo hidden (format only makes sense for documents).
        """
        is_documents = view_index == getattr(self, "_TAB_INDEX_DOCUMENTS", -1)
        is_claude = view_index == getattr(self, "_TAB_INDEX_CLAUDE", -1)

        self.format_combo.setVisible(is_documents)
        self.proxy_status_label.setVisible(is_claude)
        self.activity_tile.setVisible(is_claude)

    def _sync_format_combo_to_current_document(self) -> None:
        """Point the format combo at the active document's format.

        Called from ``_on_editor_tab_changed`` and at startup. We
        block the combo's ``currentTextChanged`` signal during the
        assignment so it doesn't fire ``_on_format_combo_changed``
        for what's effectively a UI update, not a user action.

        If no editor tab is open the combo is left at its previous
        value — it shouldn't be visible in that state anyway.
        """
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None or editor.document is None:
            return
        fmt = editor.document.format_type
        if self.format_combo.currentText() != fmt:
            self.format_combo.blockSignals(True)
            try:
                self.format_combo.setCurrentText(fmt)
            finally:
                self.format_combo.blockSignals(False)

    def _on_format_combo_changed(self, fmt: str) -> None:
        """Slot for ``format_combo.currentTextChanged``.

        The user has picked a new format. Apply it to the current
        document and re-render the preview.
        """
        self._set_current_format(fmt)

    def _init_terminal_menu(self) -> None:
        """Add the Terminal menu between Format and Claude.

        Mirrors the conventions used in ``_init_claude_menu`` — actions
        stored as ``self.action_*`` and always available (no proxy-state
        gating).

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

        As of the menu merge, the Claude menu owns everything
        Claude-related *and* everything that used to be in the Server
        menu: session creation, proxy lifecycle (Start / Stop Proxy),
        view toggles (Show Dashboard / Show Recent Activity / Open in
        Browser), proxy settings, the profile dropdown, and the
        profile manager.

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

        # ---- Session actions ------------------------------------------
        self.action_new_claude_session = QAction("&New Claude Session", self)
        self.action_new_claude_session.setShortcut("Ctrl+Alt+C")
        self.action_new_claude_session.triggered.connect(self._new_claude_session)
        claude_menu.addAction(self.action_new_claude_session)

        self.action_focus_claude = QAction("Focus &Claude Tab", self)
        self.action_focus_claude.setShortcut("Ctrl+Alt+Shift+C")
        self.action_focus_claude.triggered.connect(self._focus_claude_tab)
        claude_menu.addAction(self.action_focus_claude)

        claude_menu.addSeparator()

        # ---- Proxy lifecycle (absorbed from the old Server menu) ------
        self.action_start_proxy = QAction("Start &Proxy", self)
        self.action_start_proxy.triggered.connect(self._start_server)
        claude_menu.addAction(self.action_start_proxy)

        self.action_stop_proxy = QAction("Stop P&roxy", self)
        self.action_stop_proxy.setEnabled(False)
        self.action_stop_proxy.triggered.connect(self._stop_server)
        claude_menu.addAction(self.action_stop_proxy)

        claude_menu.addSeparator()

        # ---- View toggles (absorbed from the old Server menu) ---------
        # ``Show Dashboard`` is checkable so the menu state mirrors the
        # currently-active inner tab in the Claude container.
        self.action_show_dashboard = QAction("Show &Dashboard", self)
        self.action_show_dashboard.setCheckable(True)
        self.action_show_dashboard.setChecked(True)
        self.action_show_dashboard.triggered.connect(self._show_dashboard_panel)
        claude_menu.addAction(self.action_show_dashboard)

        self.action_show_recent = QAction("Show &Recent Activity", self)
        self.action_show_recent.setCheckable(True)
        self.action_show_recent.setChecked(False)
        self.action_show_recent.triggered.connect(self._show_recent_activity)
        claude_menu.addAction(self.action_show_recent)

        self.action_open_browser = QAction("Open in &Browser", self)
        self.action_open_browser.setEnabled(False)
        self.action_open_browser.triggered.connect(self._open_dashboard_external)
        claude_menu.addAction(self.action_open_browser)

        claude_menu.addSeparator()

        # ---- Profile submenu ------------------------------------------
        # "Profile" now hosts three things: the active-profile
        # switcher (radio group of profile names, Default pinned at
        # the top), the proxy Settings dialog (the active profile's
        # bind/port/config_dir/model_endpoints), and the Manage
        # Profiles list dialog. Settings used to be a top-level
        # Claude menu item; the user moved it under Profiles because
        # the proxy's network identity is now a per-profile concern.
        self.profile_menu = QMenu("&Profile", self)
        self.profile_group = QActionGroup(self)
        self.profile_group.setExclusive(True)
        claude_menu.addMenu(self.profile_menu)

        # "Settings…" inside the Profile submenu — edits the active
        # profile. Now that bind/port/config_dir live on the
        # profile, this is a thin alias for the profile editor
        # opened on the active profile.
        self.action_proxy_settings = QAction("&Settings…", self)
        self.action_proxy_settings.triggered.connect(self._show_proxy_settings)
        self.profile_menu.addAction(self.action_proxy_settings)

        self.profile_menu.addSeparator()

        self.action_manage_profiles = QAction("&Manage Profiles…", self)
        self.action_manage_profiles.triggered.connect(self._show_profile_editor)
        self.profile_menu.addAction(self.action_manage_profiles)

        # Populate on first show (lazy — avoids spinning up the
        # ProfileStore during cold start when the user might never
        # open the Claude tab). The switcher goes *below* the
        # static Settings / Manage entries so the radio list is
        # always anchored to the bottom of the submenu.
        self.profile_menu.aboutToShow.connect(self._refresh_profile_menu)

    def _refresh_profile_menu(self) -> None:
        """Rebuild the profile radio list from the on-disk store.

        Idempotent — called whenever the menu is about to show and
        after every edit. The checkable actions live inside
        ``self.profile_group`` so exactly one is checked (the active
        profile); switching another triggers ``_set_active_profile``.

        The static entries at the top of the submenu (Settings,
        Manage Profiles) are *not* touched — only the dynamic radio
        entries below them are rebuilt.
        """
        # Tear out the existing radio actions from the group + menu.
        for action in list(self.profile_group.actions()):
            self.profile_group.removeAction(action)
            self.profile_menu.removeAction(action)

        store = self._profile_store()
        profiles = store.load()
        active_name = store.get_active_name() or (
            profiles[0].name if profiles else None
        )

        for p in profiles:
            label = p.name
            if getattr(p, "is_default", False) or p.name == "Default":
                label = f"{p.name}  (default)"
            act = QAction(label, self)
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
        """Handle editor tab change.

        Two side effects beyond the preview-pane sync:

        * ``_sync_format_combo_to_current_document`` — the status-bar
          format selector should reflect the *current* document's
          format, not the last one the user picked.
        * The combo's signal is blocked during the sync so a tab
          switch doesn't trigger an unwanted re-render.
        """
        if index in self._tab_pairs:
            self.preview_tabs.setCurrentIndex(self._tab_pairs[index])
        # Keep the format combo pointed at the active document.
        self._sync_format_combo_to_current_document()

    def _on_preview_tab_changed(self, index: int) -> None:
        """Handle preview tab change."""
        # Keep editor in sync
        pass

    def _apply_view_state(self) -> None:
        """Apply the current split/preview visibility state to the UI.

        The two toggles are independent:

        * ``split_enabled`` controls whether the splitter shows two
          panes. When on, the right pane (``preview_tabs``) is visible;
          when off, it's hidden and the splitter collapses to one pane
          (the editor only).
        * ``preview_visible`` controls whether the right pane has any
          content. When on, ``_toggle_preview`` has already ensured a
          preview tab exists for the current editor; when off, the
          right pane may be empty (no preview tab has been created
          yet) — and that's fine, the user is signalling they want the
          pane space but not the rendered content.

        This decoupling is what lets ``Toggle Split`` work on its own
        — historically the right pane was only shown when *both* were
        true, so toggling Split with Preview off did nothing visible.
        """
        # Right pane exists iff the split is on. The preview content
        # is created on demand when Toggle Preview is first enabled.
        self.preview_tabs.setVisible(self.split_enabled)

        # When the right pane is visible, redistribute the splitter
        # to a 50/50 split. ``setStretchFactor(1, 1)`` on both children
        # only governs how the splitter *grows* on resize; it doesn't
        # initialise the current sizes, so after a hide→show cycle
        # the right pane would inherit the stale size of the moment it
        # was hidden (typically 0). Pin the sizes to an even split
        # every time the right pane becomes visible.
        if self.split_enabled:
            total = max(1, self.splitter.width())
            half = total // 2
            self.splitter.setSizes([half, total - half])

        # When the right pane is visible and a preview tab exists for
        # the current editor, sync the preview-tab selection to follow
        # editor changes — the user expects switching editors to
        # switch the rendered preview.
        if self.split_enabled:
            current = self.editor_tabs.currentIndex()
            if current in self._tab_pairs:
                self.preview_tabs.setCurrentIndex(self._tab_pairs[current])

    def _toggle_split(self) -> None:
        """Toggle the split view on/off.

        Splitting only shows/hides the second (preview) panel. The preview
        preference itself is independent, so toggling split back on restores
        the preview if it was enabled. ``_apply_view_state`` redistributes
        the splitter to a 50/50 split when the right pane becomes visible.
        """
        self.split_enabled = not self.split_enabled
        self.action_toggle_split.setChecked(self.split_enabled)
        self._apply_view_state()

    def _toggle_preview(self) -> None:
        """Toggle the preview visibility."""
        self.preview_visible = not self.preview_visible

        if self.preview_visible:
            # Preview lives inside the split, so the split must be on
            self.split_enabled = True
            self.action_toggle_split.setChecked(True)

            # Ensure the current editor has a preview tab
            current = self.editor_tabs.currentIndex()
            if current >= 0 and current not in self._tab_pairs:
                editor = self.editor_tabs.widget(current)
                self._create_preview_for(current, editor.document)

        self.action_toggle_preview.setChecked(self.preview_visible)
        self._apply_view_state()

    def _set_split_orientation(self, orientation: Qt.Orientation) -> None:
        """Set the split orientation (horizontal or vertical)."""
        self.splitter.setOrientation(orientation)

    def _set_current_format(self, format_type: str) -> None:
        """Set the current document's format and re-render the preview.

        The menu used to call this; the status-bar ``QComboBox`` is now
        the only caller (see ``_init_status_bar``). The combo fires
        ``currentTextChanged``; we block the sync signal back into the
        combo so the assignment doesn't bounce. If no editor tab is
        active the change is a no-op.
        """
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None or editor.document is None:
            return
        editor.document.format_type = format_type
        # Trigger a re-render of the preview pane (if the user has
        # enabled Preview) and a fresh syntax highlighter on the editor.
        # ``_on_editor_content_changed`` does the preview re-render
        # using the *current* content; we feed it through directly.
        self._on_editor_content_changed(current_idx, editor.document)
        # Update the highlighter — set_format alone doesn't rebind it
        # for the currently-displayed document. The editor widget
        # installs the highlighter in ``_update_highlighter`` on focus
        # changes; safest path is to call the same hook.
        if hasattr(editor, "_update_highlighter"):
            editor._update_highlighter()

    # ------------------------------------------------------------------
    # Embedded proxy control
    # ------------------------------------------------------------------
    def _start_server(self) -> None:
        """Start (or restart) the embedded proxy on its own QThread.

        Safe to call when the thread is already running — the early return
        prevents a double-start. The thread's bind address, port, config
        dir, and model endpoints are read from the **active profile**
        (the per-profile proxy settings replaced the legacy
        ``ProxyConfig``). The profile is the single source of truth:
        switching active profile naturally causes a restart with new
        routing and bind settings.
        """
        if self.proxy_thread is not None and self.proxy_thread.isRunning():
            return

        # Resolve the active profile. ``ensure_default`` guarantees we
        # always have at least one, even on a fresh install.
        active = self._profile_store().get_active() or self._profile_store().ensure_default()
        host = active.bind_address or "127.0.0.1"
        port = int(active.port or 3001)
        config_dir_path = (
            Path(active.config_dir) if active.config_dir else None
        )
        # ``default_endpoint`` lives in ``claude_settings`` until
        # Phase 3 (it's not a first-class profile field yet).
        default_endpoint = ""
        if isinstance(active.claude_settings, dict):
            default_endpoint = str(
                active.claude_settings.get("proxy_default_endpoint", "") or ""
            )
        # Flatten ``model_endpoints`` (``dict[str, ModelEndpoint]``) into
        # ``dict[str, str]`` for the proxy thread — the proxy only
        # needs upstream URLs at routing time. Each endpoint names
        # a provider (``provider_name``); the URL is resolved
        # through ``active.providers`` here. Per-endpoint credential
        # selection lives on the profile; the session launcher
        # resolves the right key via ``resolve_api_key``.
        proxy_endpoints: dict[str, str] = {}
        providers_map: dict[str, object] = dict(active.providers or {})
        for model, ep in (active.model_endpoints or {}).items():
            url: str = ""
            if isinstance(ep, str):
                # Legacy: bare URL strings (older profiles pre-dating
                # the ModelEndpoint/Provider split).
                url = ep
            else:
                provider_name = getattr(ep, "provider_name", None) or ""
                if provider_name and provider_name in providers_map:
                    provider = providers_map[provider_name]
                    url = getattr(provider, "url", "") or ""
                else:
                    # ``ep.url`` is a backward-compat fallback — newer
                    # profiles store ``provider_name`` only, but if
                    # an endpoint somehow still carries a literal URL
                    # (e.g. from a half-migrated on-disk file) we
                    # honour it instead of dropping the route.
                    url = getattr(ep, "url", "") or ""
            if url:
                proxy_endpoints[str(model)] = str(url)

        self.proxy_host = host
        self.proxy_port = port

        self.proxy_status_label.setText(
            f"Proxy: starting on http://{host}:{port}…"
        )
        self.action_start_proxy.setEnabled(False)
        self.action_stop_proxy.setEnabled(True)

        thread = ProxyServerThread(
            host=host,
            port=port,
            storage_path=default_db_path(),
            config_dir=config_dir_path,
            model_endpoints=proxy_endpoints,
            default_endpoint=default_endpoint or None,
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
        self.action_stop_proxy.setEnabled(False)
        self.proxy_thread.request_stop()

    def _on_proxy_started(self, port: int) -> None:
        """Called by the worker thread once uvicorn is listening."""
        # ``port`` is the actual bound port — equals the requested one
        # unless we ever expose port=0 in the settings dialog.
        self.proxy_port = port
        self.proxy_status_label.setText(f"Proxy: running on {self.proxy_host}:{port}")
        self.action_start_proxy.setEnabled(True)
        self.action_stop_proxy.setEnabled(True)
        self.action_show_dashboard.setEnabled(True)
        self.action_show_recent.setEnabled(True)
        self.action_open_browser.setEnabled(True)
        # Kick off the activity tile's polling against the live proxy.
        if self.activity_tile is not None:
            self.activity_tile.set_base_url(f"http://{self.proxy_host}:{port}")
        # The Claude container owns the dashboard and recent-activity
        # views; routing the URL through it means the container decides
        # whether to lazily build the heavy DashboardView.
        if self.claude_container is not None:
            self.claude_container.set_base_url(f"http://{self.proxy_host}:{port}")
        self.status.showMessage(
            f"Proxy ready — point Claude Code at http://{self.proxy_host}:{port}",
            5000,
        )
        # Flush any Claude session that was queued while the proxy
        # was starting. The container holds at most one pending
        # launch; if nothing was queued this is a no-op.
        if self.claude_container is not None:
            self.claude_container.flush_pending_launch()

    def _on_proxy_failed(self, msg: str) -> None:
        """Called when the thread or uvicorn fails to start."""
        self.proxy_status_label.setText("Proxy: failed")
        self.action_start_proxy.setEnabled(True)
        self.action_stop_proxy.setEnabled(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_recent.setEnabled(False)
        self.action_open_browser.setEnabled(False)
        QMessageBox.critical(self, "Proxy Error", msg)

    def _on_proxy_stopped(self) -> None:
        """Called after the lifespan has fully torn down."""
        self.proxy_status_label.setText("Proxy: stopped")
        self.action_start_proxy.setEnabled(True)
        self.action_stop_proxy.setEnabled(False)
        self.action_show_dashboard.setEnabled(False)
        self.action_show_recent.setEnabled(False)
        self.action_open_browser.setEnabled(False)
        # Stop polling — the URL is now invalid. The container handles
        # both the dashboard view and the recent-activity view.
        if self.activity_tile is not None:
            self.activity_tile.stop()
        if self.claude_container is not None:
            self.claude_container.stop_polling()
        # Drop our reference to the finished QThread. Qt's event loop
        # will then clean up the underlying C++ object on the next pass.
        # Without this, closeEvent later would touch a dangling object.
        self.proxy_thread = None
        # If the user just edited settings while the proxy was running,
        # restart it now with the new configuration.
        if getattr(self, "_restart_after_stop", False):
            self._restart_after_stop = False
            QTimer.singleShot(0, self._start_server)

    def _focus_proxy_tab(self) -> None:
        """Bring the Claude tab to the front and switch to the inner
        Dashboard sub-tab.

        Called by the activity tile in the status bar. With the
        Dashboard now living inside the Claude container, the click
        surfaces the same primary view it always did.
        """
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
        if self.claude_container is not None:
            self.claude_container.show_dashboard()

    def _show_recent_activity(self) -> None:
        """Switch to the Claude tab and select the Recent Activity pane.

        Wired to ``Claude → Show Recent Activity`` (was
        ``Server → Show Recent Activity`` before the menu merge).
        """
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
        if self.claude_container is not None:
            self.claude_container.show_recent_activity()

    def _show_dashboard_panel(self) -> None:
        """Slot for ``Claude → Show Dashboard``."""
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)
        if self.claude_container is not None:
            self.claude_container.show_dashboard()

    def _on_view_tab_changed(self, index: int) -> None:
        """Sync the inner Claude tab state with the active view tab.

        The Claude tab now owns both Dashboard and Recent Activity as
        inner tabs, so the top-level ``_on_view_tab_changed`` only
        needs to handle focus transfer (not action-checked state —
        those actions drive the inner tab via ``_show_*`` above).

        Without the focus transfer the ``QTabBar`` keeps focus after a
        click on a tab title and subsequent keystrokes go to it
        instead of the visible widget (this is the root cause of
        "typing in the terminal does nothing" when the user clicks the
        Terminals tab).
        """
        # Tab-conditional visibility for status-bar permanent widgets.
        # Format combo lives on Documents; proxy status + activity tile
        # live on Claude. Terminals shows neither.
        self._update_status_bar_visibility(index)

        # If the Terminals tab just became active, focus the currently
        # selected session's text edit. ``focus_active_session`` is a
        # no-op when there are no sessions yet.
        if index == getattr(self, "_TAB_INDEX_TERMINALS", -1):
            if self.terminal_container is not None:
                self.terminal_container.focus_active_session()
        # Same for Claude — without the focus transfer, keystrokes
        # after switching tabs would go to the QTabBar (Alt+arrows
        # become tab-navigation shortcuts, etc.). The container's
        # focus_active_session is a no-op when the inner tab is one
        # of the pinned tabs (Dashboard / Recent Activity).
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
        """Open the Manage Profiles list dialog.

        Replaces the old "pick one and edit it" flow. The dialog
        shows every profile, with the Default pinned at the top,
        and exposes Add / Edit / Delete actions. Edit opens the
        per-profile editor (:class:`ProfileEditorDialog`).
        """
        from src.ui.claude.profile_editor_dialog import ManageProfilesDialog

        store = self._profile_store()
        dlg = ManageProfilesDialog(store, parent=self)
        # Snapshot the active profile name so we can detect edits to
        # the active profile and trigger a proxy restart after the
        # dialog closes. The dialog itself doesn't restart the proxy
        # — that's a window-level concern.
        prev_active = store.get_active_name()
        was_running = self._is_proxy_running()
        dlg.exec()
        # The dialog mutates the store directly; refresh the menu
        # so the radio group reflects any adds/deletes.
        self._refresh_profile_menu()
        new_active = store.get_active_name()
        # Restart the proxy if the active profile's bind/port/
        # endpoints/credentials were edited while it was running.
        if was_running and new_active == prev_active:
            # Same active profile but the editor may have changed
            # its bind/port/config_dir/model_endpoints. The cheapest
            # way to apply is a stop+start cycle.
            self.status.showMessage(
                "Restarting proxy to apply profile changes…", 3000
            )
            self._restart_after_stop = True
            self._stop_server()

    def _show_proxy_settings(self) -> None:
        """Open the active profile's editor.

        Settings used to be a top-level menu item editing the
        legacy ``ProxyConfig``. After the move to per-profile
        proxy settings, "Settings…" inside the Profile submenu is
        just the editor opened on the active profile — the proxy's
        network identity (bind address, port, config dir) and
        model routing all live on the profile now.
        """
        store = self._profile_store()
        active = store.get_active()
        if active is None:
            active = store.ensure_default()
        was_running = self._is_proxy_running()
        dlg = ProfileEditorDialog(
            copy.deepcopy(active),
            credential_store=store.credential_store,
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        edited = dlg.to_profile()
        if not edited.name:
            return
        store.upsert(edited)
        self.status.showMessage(f"Saved profile {edited.name!r}.", 3000)
        self._refresh_profile_menu()
        # Apply proxy-level changes: a stop+start cycle picks up the
        # new bind/port/config_dir/model_endpoints. The new code path
        # is gated on the proxy actually running, same as before.
        if was_running:
            self.status.showMessage(
                "Restarting proxy to apply profile changes…", 3000
            )
            self._restart_after_stop = True
            self._stop_server()

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
        if self.terminal_container is not None:
            # Shut down all PTY sessions BEFORE the proxy thread so that
            # any child shells that talk to the proxy get a chance to
            # disconnect cleanly. Container.shutdown() is idempotent and
            # bounded — same philosophy as the proxy thread teardown.
            self.terminal_container.shutdown()
        if self.claude_container is not None:
            # Shuts down the Claude Code sessions AND the embedded
            # recent-activity view (polling). The dashboard view is
            # lazy and belongs to the container; if it was never built
            # nothing needs to be torn down.
            self.claude_container.shutdown()
        if thread is not None and thread.isRunning():
            thread.request_stop()
            # Give uvicorn up to 3s to drain; force-terminate otherwise.
            if not thread.wait(3000):
                thread.terminate()
                thread.wait(1000)
        super().closeEvent(event)
