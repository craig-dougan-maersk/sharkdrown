"""Container page for the "Claude" top-level tab.

Hosts a closable ``QTabWidget`` of ``TerminalSessionView`` instances
running the ``claude`` CLI, plus a toolbar with a ``+ New Session``
button. Reuses the terminal widget as-is — Claude Code is just another
PTY child process with a profile-specific environment.

This widget is the page inserted into ``view_tabs`` as the 5th top-level
tab in ``src/ui/main_window.py``.

Lifecycle of a new session
--------------------------
``new_session()`` validates the host environment (POSIX + ``claude`` on
PATH), then either launches immediately if the embedded proxy is
already running, or queues the launch and asks the main window to
start the proxy. The main window flushes the queue when its proxy
thread emits ``started_ok``.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.config_dir import write_profile_config
from src.profiles.env_builder import build_claude_session_env
from src.profiles.models import ClaudeProfile
from src.profiles.paths_helpers import profile_config_base_dir
from src.profiles.store import ProfileStore
from src.proxy.paths import data_dir
from src.ui.terminal.terminal_session_view import TerminalSessionView


@dataclass
class _PendingLaunch:
    """One deferred new-session request waiting for the proxy to come up.

    Held in a single-element slot on :class:`ClaudeContainerView`. Only
    one queued launch is meaningful — the user clicks "New Session"
    once and waits; if they click again while the proxy is still
    starting we'd just discard the second request (the GUI feels
    unresponsive for ~1-2 s, which matches the underlying reality).
    """
    profile: ClaudeProfile
    api_key: str | None


class ClaudeContainerView(QWidget):
    """Page widget: toolbar + closable tab strip of Claude Code sessions.

    The container owns the tab strip and the per-session lifecycle, but
    *not* the embedded proxy — that's owned by ``MainWindow``. The
    container talks to the main window through three callbacks set in
    :meth:`set_proxy_callbacks`:

    * ``is_proxy_running`` → bool — current proxy state
    * ``get_proxy_base_url`` → str — empty if not running
    * ``request_proxy_start`` → None — async; will emit
      ``proxy_ready_signal`` when listening

    If ``set_proxy_callbacks`` is never called, ``new_session`` shows
    an error message and returns ``None`` — this lets the widget be
    instantiated standalone (e.g. in tests) without a main window.
    """

    # Emitted when a queued session has been launched (typically right
    # after the proxy signals it's ready). Useful for tests and for
    # the main window to update its status bar.
    session_launched = Signal(object)  # the TerminalSessionView
    proxy_requested = Signal()         # ask the main window to start the proxy

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sessions: list[TerminalSessionView] = []
        # Pending single-launch slot. See :class:`_PendingLaunch`.
        self._pending: _PendingLaunch | None = None
        # Callbacks installed by MainWindow. Defaults are "no proxy"
        # so the widget is usable in tests.
        self._is_proxy_running: Callable[[], bool] = lambda: False
        self._get_proxy_base_url: Callable[[], str] = lambda: ""
        self._request_proxy_start: Callable[[], None] = lambda: None
        # Profile store — created lazily so the widget is importable
        # without a QApplication (the data_dir() call requires one).
        self._profile_store: ProfileStore | None = None
        # Test hook: when True, ``new_session`` skips ``QMessageBox``
        # warnings so headless tests don't block on modal dialogs.
        self._suppress_dialogs = False
        self._build_ui()

    # ---- public API --------------------------------------------------------

    def set_proxy_callbacks(
        self,
        *,
        is_proxy_running: Callable[[], bool],
        get_proxy_base_url: Callable[[], str],
        request_proxy_start: Callable[[], None],
    ) -> None:
        """Install the main window's proxy hooks.

        Called once during MainWindow construction. The container
        never calls these from anywhere except ``new_session``, so a
        late install is safe — any session launched before the
        callbacks were installed would already have used whatever
        defaults were active at the time.
        """
        self._is_proxy_running = is_proxy_running
        self._get_proxy_base_url = get_proxy_base_url
        self._request_proxy_start = request_proxy_start

    @property
    def profile_store(self) -> ProfileStore:
        """Lazy-init the ProfileStore (requires a QApplication for paths)."""
        if self._profile_store is None:
            self._profile_store = ProfileStore(
                path=data_dir() / "profiles.json",
            )
        return self._profile_store

    def new_session(self, profile: ClaudeProfile | None = None) -> TerminalSessionView | None:
        """Spawn a new Claude Code session and add it as a tab.

        Returns the view on success, ``None`` if a precondition
        failed (no profile, no ``claude`` binary, proxy can't start).
        When the proxy isn't running yet, queues the launch and
        asks the main window to start the proxy — the actual
        ``addTab`` happens later via :meth:`flush_pending_launch`.
        """
        # ---- preconditions ------------------------------------------------
        if not hasattr(os, "fork"):
            if not self._suppress_dialogs:
                QMessageBox.warning(
                    self,
                    "Claude sessions unsupported",
                    "Claude Code sessions require a POSIX PTY (fork()). "
                    "This build of the app is running on a non-POSIX "
                    "platform (e.g. Windows).",
                )
            return None
        claude_path = shutil.which("claude")
        if not claude_path:
            if not self._suppress_dialogs:
                QMessageBox.warning(
                    self,
                    "Claude CLI not found",
                    "`claude` was not found on PATH.\n\n"
                    "Install Claude Code: https://claude.com/download\n"
                    "Then restart the app or click + New Session again.",
                )
            return None

        # Pick the active profile if the caller didn't specify one.
        if profile is None:
            profile = self.profile_store.get_active()
        if profile is None:
            if not self._suppress_dialogs:
                QMessageBox.warning(
                    self,
                    "No profile configured",
                    "No Claude profile is configured. Open "
                    "Claude → Manage Profiles… to create one.",
                )
            return None

        api_key = None
        if profile.api_key_ref:
            api_key = self.profile_store.credential_store.get(profile.api_key_ref)
            if not api_key:
                if not self._suppress_dialogs:
                    QMessageBox.warning(
                        self,
                        "API key missing",
                        f"Profile {profile.name!r} references credential "
                        f"{profile.api_key_ref!r} but it has no value stored.\n\n"
                        "Open Claude → Manage Profiles… to set the key.",
                    )
                # Fall through with the sentinel — the user has been
                # warned and can still launch the session to interact
                # with Claude Code's own auth flow.

        # ---- proxy gate ---------------------------------------------------
        if not self._is_proxy_running():
            # Queue + ask main window to start the proxy. The actual
            # launch happens in flush_pending_launch() when the proxy
            # signals it's ready.
            self._pending = _PendingLaunch(profile=profile, api_key=api_key)
            self.proxy_requested.emit()
            return None

        return self._launch_session(profile, api_key=api_key)

    def flush_pending_launch(self) -> TerminalSessionView | None:
        """Launch the queued session (if any). Called from main window
        when the proxy signals ``started_ok``.

        Returns the new view, or ``None`` if no launch was queued or
        the launch itself failed.
        """
        pending = self._pending
        if pending is None:
            return None
        self._pending = None
        return self._launch_session(pending.profile, api_key=pending.api_key)

    def focus_active_session(self) -> None:
        """Focus the active session's text edit. Called by MainWindow
        when the Claude tab becomes the current top-level tab — same
        pattern as TerminalContainerView.focus_active_session.
        """
        widget = self.session_tabs.currentWidget()
        if isinstance(widget, TerminalSessionView):
            widget.focus_terminal()

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def shutdown(self) -> None:
        """Idempotent full-shutdown of every Claude session."""
        for view in list(self._sessions):
            try:
                view.shutdown()
            except Exception:  # noqa: BLE001
                pass

    # ---- internals ---------------------------------------------------------

    def _launch_session(
        self,
        profile: ClaudeProfile,
        *,
        api_key: str | None,
    ) -> TerminalSessionView:
        """Actually fork the claude process in a PTY and add a tab.

        Caller has already validated ``claude`` on PATH and the proxy
        running. This method:
          1. Writes the profile's config dir (settings.json + agents/).
          2. Assembles the env via ``build_claude_session_env``.
          3. Constructs a TerminalSessionView with the env + cwd.
          4. Wires ``exited``, addTab, focus, tracks in self._sessions.
        """
        config_dir_path = write_profile_config(
            profile, base_dir=profile_config_base_dir()
        )
        env = build_claude_session_env(
            profile=profile,
            proxy_base_url=self._get_proxy_base_url(),
            config_dir=str(config_dir_path),
            api_key=api_key,
        )

        view = TerminalSessionView(
            argv=["claude"],
            env=env,
            cwd=profile.session_cwd,
            parent=self,
        )
        view.exited.connect(lambda v=view: self._on_session_exited(v))
        # Tab title shows the profile so the user can tell two tabs of
        # the same ``claude`` binary apart at a glance.
        title = profile.name
        index = self.session_tabs.addTab(view, title)
        self.session_tabs.setCurrentIndex(index)
        self._sessions.append(view)
        self.session_tabs.tabBar().setVisible(True)
        self._update_info_label()
        view.start()
        view.focus_terminal()
        self.session_launched.emit(view)
        return view

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Toolbar ------------------------------------------------------
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(8)

        self.new_button = QToolButton()
        self.new_button.setText("+ New Session")
        self.new_button.setToolTip(
            "Open a new Claude Code session in this pane (Ctrl+Alt+C)"
        )
        self.new_button.clicked.connect(self._on_new_clicked)
        toolbar_layout.addWidget(self.new_button)

        toolbar_layout.addStretch(1)

        self.info_label = QLabel(
            "No sessions yet — click '+ New Session' to start."
        )
        self.info_label.setStyleSheet("color: gray; font-size: 12px;")
        toolbar_layout.addWidget(self.info_label)
        layout.addWidget(toolbar)

        # --- Inner tab widget (closable, movable) -------------------------
        self.session_tabs = QTabWidget()
        self.session_tabs.setTabsClosable(True)
        self.session_tabs.setMovable(True)
        self.session_tabs.tabCloseRequested.connect(self._close_session)
        # Hide the tab bar when there are no sessions.
        self.session_tabs.tabBar().setVisible(False)
        layout.addWidget(self.session_tabs, 1)

    def _on_new_clicked(self) -> None:
        # Toolbar button click → new session with the active profile.
        # The container doesn't decide which profile — that's the main
        # window's job (it owns the dropdown). It calls back into
        # new_session() via _on_new_session_requested.
        self.new_session()

    def _close_session(self, index: int) -> None:
        widget = self.session_tabs.widget(index)
        if widget is None:
            return
        try:
            widget.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.session_tabs.removeTab(index)
        if widget in self._sessions:
            self._sessions.remove(widget)
        widget.deleteLater()
        if self.session_tabs.count() == 0:
            self.session_tabs.tabBar().setVisible(False)
        self._update_info_label()

    def _on_session_exited(self, view: TerminalSessionView) -> None:
        for i in range(self.session_tabs.count()):
            if self.session_tabs.widget(i) is view:
                # Reuse the view's own title() so the suffix matches
                # what TerminalSessionView already says (e.g. "claude
                # (exited)"). The profile name is shown as the prefix
                # in _launch_session's addTab call; the view's title()
                # returns just the basename. We compromise by keeping
                # the profile name + appending "(exited)" if needed.
                title = view.title()
                if "(exited)" not in title:
                    title = f"{title} (exited)"
                self.session_tabs.setTabText(i, title)
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