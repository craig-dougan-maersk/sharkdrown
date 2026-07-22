"""Container page for the "Terminals" top-level tab.

Hosts a closable ``QTabWidget`` of ``TerminalSessionView`` instances plus a
small toolbar with a ``+ New Session`` button. This widget is the page
inserted into ``view_tabs`` as the 4th top-level tab in
``src/ui/main_window.py``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.terminal.terminal_session_view import TerminalSessionView


class TerminalContainerView(QWidget):
    """Page widget: toolbar + closable tab strip of terminal sessions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sessions: list[TerminalSessionView] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Toolbar -------------------------------------------------------
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(8)

        self.new_button = QToolButton()
        self.new_button.setText("+ New Session")
        self.new_button.setToolTip(
            "Open a new shell session in this terminal pane (Ctrl+Alt+T)"
        )
        self.new_button.clicked.connect(self.new_session)
        toolbar_layout.addWidget(self.new_button)

        toolbar_layout.addStretch(1)

        self.info_label = QLabel("No sessions yet — click '+ New Session' to start.")
        self.info_label.setStyleSheet("color: gray; font-size: 12px;")
        toolbar_layout.addWidget(self.info_label)
        layout.addWidget(toolbar)

        # --- Inner tab widget (closable, movable) -------------------------
        # Closable + movable mirrors the editor_tabs config in
        # main_window.py:103-107 so session tabs feel familiar.
        self.session_tabs = QTabWidget()
        self.session_tabs.setTabsClosable(True)
        self.session_tabs.setMovable(True)
        self.session_tabs.tabCloseRequested.connect(self._close_session)
        # Hide the tab strip when there are no sessions so the empty state
        # doesn't show a single useless tab.
        self.session_tabs.tabBar().setVisible(False)
        layout.addWidget(self.session_tabs, 1)

        # --- Alt+Left / Alt+Right session cycling -------------------------
        # Wired as QActions on the container so the shortcut context is
        # scoped to "this widget's focus chain" — Qt won't fire them when
        # the user is typing in the Documents tab or the menu bar. The
        # application-wide scope (``Qt.ApplicationShortcut``) would
        # steal Alt+arrows from any other widget that uses them.
        self.action_prev_session = QAction("Previous Session", self)
        self.action_prev_session.setShortcut(QKeySequence("Alt+Left"))
        self.action_prev_session.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.action_prev_session.triggered.connect(lambda: self._cycle_session(-1))
        self.addAction(self.action_prev_session)

        self.action_next_session = QAction("Next Session", self)
        self.action_next_session.setShortcut(QKeySequence("Alt+Right"))
        self.action_next_session.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.action_next_session.triggered.connect(lambda: self._cycle_session(+1))
        self.addAction(self.action_next_session)

    # ---- public API --------------------------------------------------------

    def new_session(self, cwd: str | None = None) -> TerminalSessionView:
        """Spawn a new shell session and add it as a tab. Returns the view."""
        view = TerminalSessionView(parent=self)
        view.exited.connect(lambda v=view: self._on_session_exited(v))
        index = self.session_tabs.addTab(view, view.title())
        self.session_tabs.setCurrentIndex(index)
        self._sessions.append(view)
        # Reveal the tab bar once we have ≥1 session.
        self.session_tabs.tabBar().setVisible(True)
        self._update_info_label()
        # Start the PTY now that the widget is parented and visible.
        view.start()
        # CRITICAL: focus the new session's text edit so typing is
        # delivered to the terminal immediately. Without this the click
        # on the toolbar button leaves focus on the button itself, and
        # subsequent keypresses are swallowed by the button (or by the
        # QTabBar that owns focus from clicking on the tab title).
        view.focus_terminal()
        return view

    def focus_active_session(self) -> None:
        """Focus the text edit of the currently-selected session.

        Called by MainWindow when the Terminals top-level tab becomes
        the current view — QTabWidget.currentChanged by itself doesn't
        transfer focus from the QTabBar to the visible widget.
        """
        widget = self.session_tabs.currentWidget()
        if isinstance(widget, TerminalSessionView):
            widget.focus_terminal()

    def _cycle_session(self, delta: int) -> None:
        """Switch to the next/previous session, wrapping at the edges.

        Wired to ``Alt+Left`` (delta=-1) and ``Alt+Right`` (delta=+1)
        from the QAction shortcuts set up in ``_build_ui``. With <2
        sessions the math reduces to a no-op (we can't cycle to a
        different tab), so the keys silently do nothing rather than
        wrap onto the same tab and steal focus from the user.

        After switching we call ``focus_active_session`` so the newly
        visible text edit receives keyboard focus — without this the
        user would have to click into the tab before typing resumes,
        which is exactly the "Alt+Right switches tabs but keystrokes
        still go to the previous one" failure mode.
        """
        count = self.session_tabs.count()
        if count <= 1:
            return
        current = self.session_tabs.currentIndex()
        # Modulo arithmetic handles wraparound in both directions —
        # ``-1 % count`` in Python is the correct positive wrap index,
        # so we don't need a separate branch for "before the first
        # tab" vs "after the last tab".
        new_index = (current + delta) % count
        self.session_tabs.setCurrentIndex(new_index)
        self.focus_active_session()

    @property
    def session_count(self) -> int:
        """Number of currently-open sessions (including exited-but-unclosed)."""
        return len(self._sessions)

    def shutdown(self) -> None:
        """Idempotent full-shutdown of every session."""
        # Snapshot the list — shutdown() may mutate it via close signals.
        for view in list(self._sessions):
            try:
                view.shutdown()
            except Exception:  # noqa: BLE001
                pass

    # ---- internals ---------------------------------------------------------

    def _close_session(self, index: int) -> None:
        """Slot for the inner tab widget's ``tabCloseRequested``."""
        widget = self.session_tabs.widget(index)
        if widget is None:
            return
        # Shut the session down BEFORE removing the tab so any UI cleanup
        # (e.g. setReadOnly) still has a parent container to talk to.
        try:
            widget.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.session_tabs.removeTab(index)
        if widget in self._sessions:
            self._sessions.remove(widget)
        # Schedule the C++ object for deletion once Qt has finished
        # processing pending events on it.
        widget.deleteLater()
        # Hide the tab bar if we just dropped to zero sessions.
        if self.session_tabs.count() == 0:
            self.session_tabs.tabBar().setVisible(False)
        self._update_info_label()

    def _on_session_exited(self, view: TerminalSessionView) -> None:
        """Refresh the tab title when the child shell exits."""
        for i in range(self.session_tabs.count()):
            if self.session_tabs.widget(i) is view:
                self.session_tabs.setTabText(i, view.title())
                break
        self._update_info_label()

    def _update_info_label(self) -> None:
        n = self.session_count
        if n == 0:
            self.info_label.setText(
                "No sessions yet — click '+ New Session' to start."
            )
        elif n == 1:
            self.info_label.setText("1 session open")
        else:
            self.info_label.setText(f"{n} sessions open")
