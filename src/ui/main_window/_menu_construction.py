"""Menu construction mixin — File/View/Terminal/Claude menus and status bar."""

from __future__ import annotations

from PySide6.QtGui import QAction, QActionGroup, QClipboard, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMenu,
    QMessageBox,
)

from src.proxy.activity_tile import ActivityTile


class MenuConstructionMixin:
    """Builds the menu bar, status bar, and terminal session slots."""

    def _init_menu(self) -> None:
        """Initialize the menu bar."""
        from PySide6.QtCore import Qt

        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("&File")

        self.action_new = QAction("&New", self)
        self.action_new.setShortcut("Ctrl+N")
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

        view_menu.addSeparator()

        # Theme submenu
        from src.ui.theme import ThemeManager, ThemeMode

        self.theme_manager = ThemeManager()
        theme_menu = view_menu.addMenu("&Theme")
        self._theme_actions: dict[ThemeMode, QAction] = {}
        for mode in ThemeMode:
            action = QAction(mode.value.capitalize(), self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, m=mode: self._apply_theme(m))
            theme_menu.addAction(action)
            self._theme_actions[mode] = action
        saved = self.theme_manager.load_saved()
        self._theme_actions[saved].setChecked(True)

    def _apply_theme(self, mode) -> None:
        from src.ui.theme import ThemeMode

        self.theme_manager.apply(mode)
        for m, action in self._theme_actions.items():
            action.setChecked(m == mode)

    def _init_status_bar(self) -> None:
        """Initialize the status bar."""
        self.status = self.statusBar()
        self.status.showMessage("Ready")

        self.format_combo = QComboBox()
        self.format_combo.addItems(["markdown", "json", "yaml"])
        self.format_combo.setToolTip(
            "Format for the current document.\n"
            "Drives syntax highlighting and preview rendering."
        )
        self.format_combo.currentTextChanged.connect(self._on_format_combo_changed)
        self.status.addPermanentWidget(self.format_combo)

        self.proxy_status_label = QLabel("Proxy: stopped")
        self.status.addPermanentWidget(self.proxy_status_label)

        self.activity_tile = ActivityTile()
        self.activity_tile.clicked.connect(self._focus_proxy_tab)
        self.status.addPermanentWidget(self.activity_tile)

        self.proxy_status_label.setVisible(False)
        self.activity_tile.setVisible(False)

    def _update_status_bar_visibility(self, view_index: int) -> None:
        """Show the right permanent widgets for the active top-level tab."""
        is_documents = view_index == getattr(self, "_TAB_INDEX_DOCUMENTS", -1)
        is_claude = view_index == getattr(self, "_TAB_INDEX_CLAUDE", -1)

        self.format_combo.setVisible(is_documents)
        self.proxy_status_label.setVisible(is_claude)
        self.activity_tile.setVisible(is_claude)

    def _sync_format_combo_to_current_document(self) -> None:
        """Point the format combo at the active document's format."""
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
        """Slot for format_combo.currentTextChanged."""
        self._set_current_format(fmt)

    def _init_terminal_menu(self) -> None:
        """Add the Terminal menu."""
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

        self.action_record_session = QAction("&Record Session", self)
        self.action_record_session.setCheckable(True)
        self.action_record_session.triggered.connect(self._toggle_recording)
        terminal_menu.addAction(self.action_record_session)

        terminal_menu.addSeparator()

        self.action_kill_all_sessions = QAction("&Kill All Sessions", self)
        self.action_kill_all_sessions.triggered.connect(self._kill_all_terminal_sessions)
        terminal_menu.addAction(self.action_kill_all_sessions)

    def _init_claude_menu(self) -> None:
        """Add the Claude menu."""
        menubar = self.menuBar()
        claude_menu = menubar.addMenu("C&laude")

        # ---- Session actions ------------------------------------------
        self.action_new_claude_session = QAction("&New Claude Session", self)
        self.action_new_claude_session.setShortcut("Ctrl+Alt+C")
        self.action_new_claude_session.setEnabled(False)  # enabled once proxy signals started_ok
        self.action_new_claude_session.triggered.connect(self._new_claude_session)
        claude_menu.addAction(self.action_new_claude_session)

        self.action_focus_claude = QAction("Focus &Claude Tab", self)
        self.action_focus_claude.setShortcut("Ctrl+Alt+Shift+C")
        self.action_focus_claude.triggered.connect(self._focus_claude_tab)
        claude_menu.addAction(self.action_focus_claude)

        claude_menu.addSeparator()

        # ---- Proxy lifecycle ------------------------------------------
        self.action_start_proxy = QAction("Start &Proxy", self)
        self.action_start_proxy.triggered.connect(self._start_server)
        claude_menu.addAction(self.action_start_proxy)

        self.action_stop_proxy = QAction("Stop P&roxy", self)
        self.action_stop_proxy.setEnabled(False)
        self.action_stop_proxy.triggered.connect(self._stop_server)
        claude_menu.addAction(self.action_stop_proxy)

        claude_menu.addSeparator()

        # ---- View toggles ---------------------------------------------
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
        self.profile_menu = QMenu("&Profile", self)
        self.profile_group = QActionGroup(self)
        self.profile_group.setExclusive(True)
        claude_menu.addMenu(self.profile_menu)

        self.action_proxy_settings = QAction("&Settings…", self)
        self.action_proxy_settings.triggered.connect(self._show_proxy_settings)
        self.profile_menu.addAction(self.action_proxy_settings)

        self.profile_menu.addSeparator()

        self.action_manage_profiles = QAction("&Manage Profiles…", self)
        self.action_manage_profiles.triggered.connect(self._show_profile_editor)
        self.profile_menu.addAction(self.action_manage_profiles)

        self.profile_menu.aboutToShow.connect(self._refresh_profile_menu)

    # ------------------------------------------------------------------
    # Terminal session slots
    # ------------------------------------------------------------------

    def _new_terminal_session(self) -> None:
        """Open a new shell session in the Terminals tab and switch to it."""
        if self.terminal_container is None:
            return
        self.terminal_container.new_session()
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_TERMINALS)

    def _focus_terminals_tab(self) -> None:
        """Slot for Terminal -> Focus Terminals Tab."""
        idx = getattr(self, "_TAB_INDEX_TERMINALS", None)
        if idx is not None:
            self.view_tabs.setCurrentIndex(idx)

    def _active_terminal_session(self):
        """Return the active TerminalSessionView or None."""
        if self.terminal_container is None:
            return None
        sess = self.terminal_container.session_tabs.currentWidget()
        from src.ui.terminal.terminal_session_view import TerminalSessionView
        if isinstance(sess, TerminalSessionView):
            return sess
        return None

    def _copy_terminal_selection(self) -> None:
        """Menu-bar hook for Terminal -> Copy."""
        sess = self._active_terminal_session()
        if sess is None:
            return
        sess.text_edit._copy_selection()

    def _paste_to_terminal(self) -> None:
        """Menu-bar hook for Terminal -> Paste."""
        sess = self._active_terminal_session()
        if sess is None:
            return
        sess.text_edit._paste_from(mode=QClipboard.Clipboard)

    def _select_all_terminal(self) -> None:
        """Menu-bar hook for Terminal -> Select All."""
        sess = self._active_terminal_session()
        if sess is None:
            return
        sess.text_edit.select_all()

    def _toggle_recording(self, checked: bool) -> None:
        """Toggle session recording on/off for the active terminal."""
        sess = self._active_terminal_session()
        if sess is None:
            self.action_record_session.setChecked(False)
            self.status.showMessage("No active terminal session.", 3000)
            return
        if checked:
            path = sess.start_recording()
            self.status.showMessage(f"Recording to {path}", 5000)
        else:
            path = sess.stop_recording()
            if path:
                self.status.showMessage(f"Recording saved: {path}", 5000)

    def _kill_all_terminal_sessions(self) -> None:
        """Slot for Terminal -> Kill All Sessions."""
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
