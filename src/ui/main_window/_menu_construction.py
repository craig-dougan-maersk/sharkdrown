"""Menu construction mixin — Main/View/Code/Diagrams/Terminal/Claude
menus and status bar."""

from __future__ import annotations

from PySide6.QtGui import QAction, QActionGroup, QClipboard, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QMenu,
    QMessageBox,
    QSlider,
)
from PySide6.QtCore import Qt

from src.proxy.activity_tile import ActivityTile


class MenuConstructionMixin:
    """Builds the menu bar, status bar, and terminal session slots."""

    def _init_menu(self) -> None:
        """Initialize the menu bar (Main + View).

        ``Main`` is the former ``File`` menu. Its document actions
        (New/Open/Save/Save All/Close) now live on the ``Code`` menu —
        they act on source documents, so they belong beside the other
        code actions. What remains on ``Main`` is application-level:
        quitting the app.

        ``View`` likewise shed the split-layout actions (Toggle Split
        View / Side By Side / Stacked) to ``Code``; View now governs
        theme and font chrome only.
        """
        menubar = self.menuBar()

        # Main menu (formerly "File")
        main_menu = menubar.addMenu("&Main")

        self.action_exit = QAction("E&xit", self)
        self.action_exit.setShortcut("Ctrl+Q")
        self.action_exit.triggered.connect(self.close)
        main_menu.addAction(self.action_exit)

        # View menu
        view_menu = menubar.addMenu("&View")

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

        view_menu.addSeparator()

        # Content font size (affects editor/terminal/claude, NOT UI chrome)
        self.action_increase_font = QAction("Increase Content Font", self)
        self.action_increase_font.setShortcut("Ctrl+=")
        self.action_increase_font.triggered.connect(self._increase_font_size)
        view_menu.addAction(self.action_increase_font)

        self.action_decrease_font = QAction("Decrease Content Font", self)
        self.action_decrease_font.setShortcut("Ctrl+-")
        self.action_decrease_font.triggered.connect(self._decrease_font_size)
        view_menu.addAction(self.action_decrease_font)

        self.action_reset_font = QAction("Reset Content Font", self)
        self.action_reset_font.setShortcut("Ctrl+0")
        self.action_reset_font.triggered.connect(self._reset_font_size)
        view_menu.addAction(self.action_reset_font)

        view_menu.addSeparator()

        # UI font submenu
        ui_font_menu = view_menu.addMenu("&UI Font")

        self.action_ui_font_increase = QAction("Increase UI Font", self)
        self.action_ui_font_increase.triggered.connect(self._increase_ui_font)
        ui_font_menu.addAction(self.action_ui_font_increase)

        self.action_ui_font_decrease = QAction("Decrease UI Font", self)
        self.action_ui_font_decrease.triggered.connect(self._decrease_ui_font)
        ui_font_menu.addAction(self.action_ui_font_decrease)

        self.action_ui_font_reset = QAction("Reset UI Font", self)
        self.action_ui_font_reset.triggered.connect(self._reset_ui_font)
        ui_font_menu.addAction(self.action_ui_font_reset)

        ui_font_menu.addSeparator()

        # UI font family selector
        self._ui_font_family_group = QActionGroup(self)
        self._ui_font_family_group.setExclusive(True)
        families = ["System Default", "Inter", "Segoe UI", "Helvetica Neue",
                    "SF Pro", "Roboto", "Noto Sans"]
        from PySide6.QtCore import QSettings
        settings = QSettings("SharkDrown", "App")
        saved_family = settings.value("ui_font_family", "System Default")
        for family in families:
            action = QAction(family, self)
            action.setCheckable(True)
            action.setChecked(family == saved_family)
            action.triggered.connect(
                lambda checked, f=family: self._set_ui_font_family(f)
            )
            self._ui_font_family_group.addAction(action)
            ui_font_menu.addAction(action)

    def _apply_theme(self, mode) -> None:
        from src.ui.theme import ThemeMode

        self.theme_manager.apply(mode)
        for m, action in self._theme_actions.items():
            action.setChecked(m == mode)

    # ---- content font size (Ctrl+=/-, does NOT affect UI chrome) ------------

    def _increase_font_size(self) -> None:
        self._adjust_content_font(+1)

    def _decrease_font_size(self) -> None:
        self._adjust_content_font(-1)

    def _reset_font_size(self) -> None:
        self._adjust_content_font(0, reset=True)

    def _adjust_content_font(self, delta: int, *, reset: bool = False) -> None:
        """Adjust font size for the active editor/terminal/claude session."""
        from PySide6.QtCore import QSettings

        view_index = self.view_tabs.currentIndex()
        is_documents = view_index == getattr(self, "_TAB_INDEX_DOCUMENTS", -1)
        is_terminals = view_index == getattr(self, "_TAB_INDEX_TERMINALS", -1)
        is_claude = view_index == getattr(self, "_TAB_INDEX_CLAUDE", -1)

        if is_terminals or is_claude:
            sess = self._active_terminal_session()
            if sess is not None:
                if reset:
                    sess.adjust_font_size(0)
                else:
                    sess.adjust_font_size(delta)
        elif is_documents:
            editor = self.editor_tabs.currentWidget()
            if editor is not None and hasattr(editor, "adjust_font_size"):
                if reset:
                    editor.reset_font_size()
                else:
                    editor.adjust_font_size(delta)

        # Update the status bar slider to reflect the new size.
        self._sync_font_slider()

    # ---- UI font (separate from content font) --------------------------------

    def _increase_ui_font(self) -> None:
        self._adjust_ui_font(+1)

    def _decrease_ui_font(self) -> None:
        self._adjust_ui_font(-1)

    def _reset_ui_font(self) -> None:
        self._adjust_ui_font(0, reset=True)

    def _adjust_ui_font(self, delta: int, *, reset: bool = False) -> None:
        """Adjust the application UI font size (menus, dialogs, labels)."""
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        settings = QSettings("SharkDrown", "App")
        app = QApplication.instance()
        app_font = app.font()
        base_size = settings.value("app_font_base_size", 13, type=int)

        if reset:
            app_font.setPointSize(base_size)
            settings.setValue("app_font_delta", 0)
        else:
            current_delta = settings.value("app_font_delta", 0, type=int)
            new_delta = current_delta + delta
            new_size = max(8, min(24, base_size + new_delta))
            app_font.setPointSize(new_size)
            settings.setValue("app_font_delta", new_size - base_size)
        app.setFont(app_font)

    def _set_ui_font_family(self, family: str) -> None:
        """Change the UI font family."""
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QFont

        settings = QSettings("SharkDrown", "App")
        settings.setValue("ui_font_family", family)

        app = QApplication.instance()
        font = app.font()
        if family == "System Default":
            from PySide6.QtGui import QFontDatabase
            default = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
            font.setFamily(default.family())
        else:
            font.setFamily(family)
        app.setFont(font)

    def _init_status_bar(self) -> None:
        """Initialize the status bar."""
        from PySide6.QtCore import QSettings

        self.status = self.statusBar()
        self.status.showMessage("Ready")

        # Session logging toggle — appears on the left of the permanent area.
        settings = QSettings("SharkDrown", "App")
        self.log_sessions_checkbox = QCheckBox("Log Sessions")
        self.log_sessions_checkbox.setChecked(
            settings.value("session_logging_enabled", True, type=bool)
        )
        self.log_sessions_checkbox.setToolTip(
            "When checked, new terminal and Claude sessions write\n"
            "a plain-text log to ~/.sharkdrown/logs/"
        )
        self.log_sessions_checkbox.toggled.connect(self._on_log_sessions_toggled)
        self.status.addPermanentWidget(self.log_sessions_checkbox)

        # Font size slider for editor/terminal/claude content.
        self.font_size_label = QLabel("Font:")
        self.status.addPermanentWidget(self.font_size_label)

        self.font_size_slider = QSlider(Qt.Horizontal)
        self.font_size_slider.setRange(8, 32)
        self.font_size_slider.setValue(12)
        self.font_size_slider.setFixedWidth(100)
        self.font_size_slider.setToolTip("Adjust font size for editor/terminal/Claude sessions")
        self.font_size_slider.valueChanged.connect(self._on_font_slider_changed)
        self.status.addPermanentWidget(self.font_size_slider)

        self.font_size_value_label = QLabel("12pt")
        self.font_size_value_label.setFixedWidth(32)
        self.status.addPermanentWidget(self.font_size_value_label)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["markdown", "json", "yaml", "text", "html"])
        self.format_combo.setToolTip(
            "Format for the current document.\n"
            "Drives syntax highlighting and preview rendering."
        )
        # ``activated`` fires only on user interaction (not on
        # programmatic ``setCurrentText``), which is the right signal
        # for "user picked an item". The previous ``currentTextChanged``
        # connection could leave the popup open because the slot did
        # heavy work (re-render preview, run lint) that stole focus
        # before Qt's default close-after-click handling fired.
        # Note: PySide6 only exposes the ``activated(int)`` overload,
        # not a string overload, so the slot converts index→text.
        self.format_combo.activated.connect(self._on_format_combo_activated)
        self.status.addPermanentWidget(self.format_combo)

        # Document statistics labels (visible on Documents tab only)
        self.word_count_label = QLabel("Words: 0")
        self.word_count_label.setStyleSheet("font-size: 11px; padding: 0 4px;")
        self.status.addPermanentWidget(self.word_count_label)

        self.line_count_label = QLabel("Lines: 0")
        self.line_count_label.setStyleSheet("font-size: 11px; padding: 0 4px;")
        self.status.addPermanentWidget(self.line_count_label)

        self.cursor_pos_label = QLabel("Ln 1, Col 1")
        self.cursor_pos_label.setStyleSheet("font-size: 11px; padding: 0 4px;")
        self.status.addPermanentWidget(self.cursor_pos_label)

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
        self.word_count_label.setVisible(is_documents)
        self.line_count_label.setVisible(is_documents)
        self.cursor_pos_label.setVisible(is_documents)
        self.proxy_status_label.setVisible(is_claude)
        self.activity_tile.setVisible(is_claude)
        self._sync_font_slider()

    def _sync_format_combo_to_current_document(self) -> None:
        """Point the format combo at the active document's format.

        No ``blockSignals`` is needed: the combo is connected to
        ``activated`` (user-only), so programmatic ``setCurrentText``
        won't re-enter the slot.
        """
        current_idx = self.editor_tabs.currentIndex()
        if current_idx < 0:
            return
        editor = self.editor_tabs.widget(current_idx)
        if editor is None or editor.document is None:
            return
        fmt = editor.document.format_type
        if self.format_combo.currentText() != fmt:
            self.format_combo.setCurrentText(fmt)

    def _on_format_combo_activated(self, index: int) -> None:
        """Slot for ``format_combo.activated`` — fires only on user pick.

        The ``hidePopup()`` call is a safety net: ``activated`` is
        emitted before Qt's default close-after-click, so the popup
        would otherwise remain visible while the slot's downstream
        work (preview re-render, lint) runs.
        """
        fmt = self.format_combo.itemText(index)
        self.format_combo.hidePopup()
        self._set_current_format(fmt)

    def _toggle_lint_panel(self, checked: bool) -> None:
        """Show or hide the lint results panel."""
        if hasattr(self, "lint_panel"):
            self.lint_panel.setVisible(checked)
            if checked:
                self._run_lint()

    def _on_log_sessions_toggled(self, checked: bool) -> None:
        """Persist the logging preference and update the active session."""
        from PySide6.QtCore import QSettings

        QSettings("SharkDrown", "App").setValue("session_logging_enabled", checked)
        sess = self._active_terminal_session()
        if sess is not None:
            sess.set_logging_enabled(checked)

    def _on_show_line_numbers_toggled(self, checked: bool) -> None:
        """Apply the line-number preference to every open editor.

        Iterates over ``editor_tabs`` rather than only the active
        editor so users with multiple documents open see consistent
        behaviour across all tabs. New tabs pick up the same
        preference via ``_line_numbers_enabled_pref`` in
        ``_document_ops.py``.
        """
        from PySide6.QtCore import QSettings

        checked = bool(checked)
        self._line_numbers_enabled_pref = checked
        QSettings("SharkDrown", "App").setValue(
            "line_numbers_enabled", checked
        )
        for i in range(self.editor_tabs.count()):
            editor = self.editor_tabs.widget(i)
            if editor is not None and hasattr(editor, "set_line_numbers_enabled"):
                editor.set_line_numbers_enabled(checked)

    def _on_font_slider_changed(self, value: int) -> None:
        """Set the content font to the slider's value."""
        self.font_size_value_label.setText(f"{value}pt")
        view_index = self.view_tabs.currentIndex()
        is_documents = view_index == getattr(self, "_TAB_INDEX_DOCUMENTS", -1)
        is_terminals = view_index == getattr(self, "_TAB_INDEX_TERMINALS", -1)
        is_claude = view_index == getattr(self, "_TAB_INDEX_CLAUDE", -1)

        if is_terminals or is_claude:
            sess = self._active_terminal_session()
            if sess is not None and hasattr(sess, "_base_font"):
                current = sess._base_font.pointSize()
                if current != value:
                    sess.adjust_font_size(value - current)
        elif is_documents:
            editor = self.editor_tabs.currentWidget()
            if editor is not None and hasattr(editor, "editor"):
                font = editor.editor.font()
                if font.pointSize() != value:
                    font.setPointSize(max(8, min(32, value)))
                    editor.editor.setFont(font)

    def _sync_font_slider(self) -> None:
        """Sync the font slider to the current active widget's font size."""
        size = 12
        view_index = self.view_tabs.currentIndex()
        is_documents = view_index == getattr(self, "_TAB_INDEX_DOCUMENTS", -1)
        is_terminals = view_index == getattr(self, "_TAB_INDEX_TERMINALS", -1)
        is_claude = view_index == getattr(self, "_TAB_INDEX_CLAUDE", -1)

        if is_terminals or is_claude:
            sess = self._active_terminal_session()
            if sess is not None and hasattr(sess, "_base_font"):
                size = sess._base_font.pointSize()
        elif is_documents:
            editor = self.editor_tabs.currentWidget()
            if editor is not None and hasattr(editor, "editor"):
                size = editor.editor.font().pointSize()

        self.font_size_slider.blockSignals(True)
        self.font_size_slider.setValue(size)
        self.font_size_slider.blockSignals(False)
        self.font_size_value_label.setText(f"{size}pt")

    def _init_code_menu(self) -> None:
        """Add the Code menu between View and Diagrams.

        The Code menu is the home for everything that acts on source
        documents:

        * document lifecycle — ``New``, ``Open``, ``Save``,
          ``Save All``, ``Close`` (moved off the former ``File`` menu,
          now ``Main``, which keeps only ``Exit``);
        * split layout — ``Toggle Split View``, ``Side By Side``,
          ``Stacked`` (moved off ``View``);
        * code analysis — ``Toggle Preview`` and ``Toggle Lint Panel``.

        ``View`` is left governing application chrome only (theme,
        fonts).

        Mnemonics are unique *within this menu*: N/O/S/A/C for the
        document group, T/B/K for the layout group, P/L for the
        analysis group. Duplicates would make Qt cycle candidates on
        each keypress instead of activating the item outright — note
        ``Stac&ked`` avoids colliding with ``&Save``, and ``Toggle
        &Preview`` avoids colliding with ``&Toggle Split View``.
        """
        menubar = self.menuBar()
        code_menu = menubar.addMenu("&Code")

        # ---- Document lifecycle (moved from File/Main) -----------------
        self.action_new = QAction("&New", self)
        self.action_new.setShortcut("Ctrl+N")
        self.action_new.triggered.connect(lambda _checked=False: self._new_file_with_dialog())
        code_menu.addAction(self.action_new)

        self.action_open = QAction("&Open...", self)
        self.action_open.setShortcut("Ctrl+O")
        self.action_open.triggered.connect(self._open_file)
        code_menu.addAction(self.action_open)

        self.action_save = QAction("&Save", self)
        self.action_save.setShortcut("Ctrl+S")
        self.action_save.triggered.connect(self._save_file)
        code_menu.addAction(self.action_save)

        self.action_save_all = QAction("Save &All", self)
        self.action_save_all.setShortcut("Ctrl+Shift+S")
        self.action_save_all.triggered.connect(self._save_all)
        code_menu.addAction(self.action_save_all)

        code_menu.addSeparator()

        self.action_close = QAction("&Close", self)
        self.action_close.setShortcut("Ctrl+W")
        self.action_close.triggered.connect(self._close_current_tab)
        code_menu.addAction(self.action_close)

        code_menu.addSeparator()

        # ---- Split layout (moved from View) ---------------------------
        self.action_toggle_split = QAction("&Toggle Split View", self)
        self.action_toggle_split.setShortcut("Ctrl+D")
        self.action_toggle_split.setCheckable(True)
        self.action_toggle_split.setChecked(False)
        self.action_toggle_split.triggered.connect(self._toggle_split)
        code_menu.addAction(self.action_toggle_split)

        self.action_side_by_side = QAction("Side &By Side", self)
        self.action_side_by_side.setShortcut("Ctrl+1")
        self.action_side_by_side.triggered.connect(lambda: self._set_split_orientation(Qt.Horizontal))
        code_menu.addAction(self.action_side_by_side)

        self.action_stacked = QAction("Stac&ked", self)
        self.action_stacked.setShortcut("Ctrl+2")
        self.action_stacked.triggered.connect(lambda: self._set_split_orientation(Qt.Vertical))
        code_menu.addAction(self.action_stacked)

        code_menu.addSeparator()

        # ---- Code analysis --------------------------------------------
        self.action_toggle_preview = QAction("Toggle &Preview", self)
        self.action_toggle_preview.setShortcut("Ctrl+P")
        self.action_toggle_preview.setCheckable(True)
        self.action_toggle_preview.setChecked(False)
        self.action_toggle_preview.triggered.connect(self._toggle_preview)
        code_menu.addAction(self.action_toggle_preview)

        code_menu.addSeparator()

        self.action_toggle_lint = QAction("Toggle &Lint Panel", self)
        self.action_toggle_lint.setShortcut("Ctrl+Shift+L")
        self.action_toggle_lint.setCheckable(True)
        self.action_toggle_lint.setChecked(False)
        self.action_toggle_lint.triggered.connect(self._toggle_lint_panel)
        code_menu.addAction(self.action_toggle_lint)

        code_menu.addSeparator()

        # ---- View options ------------------------------------------------
        # ``Show Line Numbers`` is a global preference, not a per-tab
        # setting, so toggling it rebroadcasts to every open editor and
        # the preference is persisted via QSettings so new tabs honour
        # it on creation (see ``_document_ops.py``).
        from PySide6.QtCore import QSettings

        settings = QSettings("SharkDrown", "App")
        self._line_numbers_enabled_pref = settings.value(
            "line_numbers_enabled", True, type=bool
        )
        self.action_show_line_numbers = QAction("Show &Line Numbers", self)
        self.action_show_line_numbers.setCheckable(True)
        self.action_show_line_numbers.setChecked(self._line_numbers_enabled_pref)
        self.action_show_line_numbers.triggered.connect(
            self._on_show_line_numbers_toggled
        )
        code_menu.addAction(self.action_show_line_numbers)

    def _init_diagrams_menu(self) -> None:
        """Add the Diagrams menu between Code and Terminal.

        Called from ``MainWindow.__init__`` between ``_init_code_menu``
        and ``_init_terminal_menu`` — ``QMenuBar.addMenu`` appends, so
        call order *is* menu-bar order.

        Shortcuts mirror the Terminal/Claude convention:
        ``Ctrl+Alt+<key>`` opens a new item, ``Ctrl+Alt+Shift+<key>``
        focuses the tab.
        """
        menubar = self.menuBar()
        diagrams_menu = menubar.addMenu("&Diagrams")

        self.action_new_diagram = QAction("&New Diagram", self)
        self.action_new_diagram.setShortcut("Ctrl+Alt+D")
        self.action_new_diagram.triggered.connect(self._new_diagram)
        diagrams_menu.addAction(self.action_new_diagram)

        self.action_focus_diagrams = QAction("Focus &Diagrams Tab", self)
        self.action_focus_diagrams.setShortcut("Ctrl+Alt+Shift+D")
        self.action_focus_diagrams.triggered.connect(self._focus_diagrams_tab)
        diagrams_menu.addAction(self.action_focus_diagrams)

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

        terminal_menu.addSeparator()

        self.action_terminal_settings = QAction("Terminal &Settings…", self)
        self.action_terminal_settings.triggered.connect(self._open_terminal_settings)
        terminal_menu.addAction(self.action_terminal_settings)

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
        # The checkable ``Show Dashboard`` / ``Show Recent Activity``
        # items now actually toggle visibility of the corresponding
        # inner tabs in ``ClaudeContainerView`` — not just navigate
        # to them. Both are unchecked by default; the user opts in to
        # the dashboard / activity pane explicitly.
        self.action_show_dashboard = QAction("Show &Dashboard", self)
        self.action_show_dashboard.setCheckable(True)
        self.action_show_dashboard.setChecked(False)
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

    # ------------------------------------------------------------------
    # Diagram slots
    # ------------------------------------------------------------------

    def _new_diagram(self) -> None:
        """Open a new diagram in the Diagrams tab and switch to it."""
        if self.diagram_container is None:
            return
        self.diagram_container.new_diagram()
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_DIAGRAMS)
        self.diagram_container.focus_active_diagram()

    def _focus_diagrams_tab(self) -> None:
        """Slot for Diagrams -> Focus Diagrams Tab."""
        idx = getattr(self, "_TAB_INDEX_DIAGRAMS", None)
        if idx is not None:
            self.view_tabs.setCurrentIndex(idx)

    def _active_terminal_session(self):
        """Return the active TerminalSessionView (terminal or Claude) or None."""
        from src.ui.terminal.terminal_session_view import TerminalSessionView

        view_index = self.view_tabs.currentIndex()
        is_claude = view_index == getattr(self, "_TAB_INDEX_CLAUDE", -1)

        if is_claude:
            claude_container = getattr(self, "claude_container", None)
            if claude_container is not None:
                widget = claude_container.tabs.currentWidget()
                if isinstance(widget, TerminalSessionView):
                    return widget
            return None

        if self.terminal_container is None:
            return None
        sess = self.terminal_container.session_tabs.currentWidget()
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

    def _open_terminal_settings(self) -> None:
        """Slot for Terminal -> Terminal Settings…"""
        from src.ui.terminal.settings_dialog import TerminalSettingsDialog

        if TerminalSettingsDialog.open_dialog(self):
            # Apply the new theme immediately to any future repaints.
            from src.ui.terminal.settings_dialog import get_terminal_theme
            from src.ui.terminal.renderer import apply_theme

            apply_theme(get_terminal_theme())
            # Force a repaint on the active terminal session so the user
            # sees the change immediately.
            sess = self._active_terminal_session()
            if sess is not None:
                sess._repaint()
            self.status.showMessage("Terminal settings saved.", 3000)
