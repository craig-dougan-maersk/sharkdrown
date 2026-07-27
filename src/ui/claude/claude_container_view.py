"""Container page for the "Claude" top-level tab.

Hosts a nested ``QTabWidget`` with three kinds of tabs:

* **Pinned** — ``Dashboard`` (index 0) and ``Recent Activity`` (index 1).
  These are always present, never closable, and always in this order.
  The first two tabs are pinned by index; their close buttons are
  hidden via ``tabBar().setTabButton(idx, RightSide, None)`` and any
  stray close-request signals are filtered out in ``_on_close_requested``.
* **Sessions** — ``Claude Code`` PTY sessions at index 2 onward.
  Each is closable via the ``×`` button and labelled with the
  profile name.

The container also owns the proxy lifecycle hooks (callbacks installed
by the main window) and lazily builds the heavy ``DashboardView`` (a
``QWebEngineView``) on first proxy start.

Lifecycle of a new session
--------------------------
``new_session()`` validates the host environment (POSIX + ``claude`` on
PATH), then either launches immediately if the embedded proxy is
already running, or queues the launch and asks the main window to
start the proxy. The main window flushes the queue when its proxy
thread emits ``started_ok``.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.profiles.config_dir import write_profile_config
from src.profiles.env_builder import build_claude_session_env, resolve_api_key
from src.profiles.models import ClaudeProfile
from src.profiles.paths_helpers import profile_config_base_dir
from src.profiles.store import ProfileStore
from src.proxy.dashboard_view import DashboardView
from src.proxy.paths import data_dir
from src.ui.recent_activity_view import RecentActivityView
from src.ui.terminal.terminal_session_view import TerminalSessionView

logger = logging.getLogger(__name__)


# Number of pinned tabs at the head of the inner ``QTabWidget``. Session
# tabs slot in at index ``_NUM_PINNED`` and above. Centralised so the
# close-request filter and the close-button hiding stay in sync.
_NUM_PINNED = 2


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
    """Page widget: toolstrip + nested tab strip (pinned + sessions).

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
        # Dashboard view is built lazily when the proxy starts — the
        # ``QWebEngineWidgets`` import is heavy and only useful once
        # we have a URL to point at.
        self.dashboard_view: DashboardView | None = None
        self._dashboard_placeholder: QLabel | None = None
        # Recent Activity view is built up-front (no heavy deps; just
        # a QTableWidget). Polling is paused until ``set_base_url``
        # is called with a real URL.
        self.recent_activity_view: RecentActivityView | None = None
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

    def set_base_url(self, url: str) -> None:
        """Point the embedded dashboard and recent-activity views at
        a freshly-started proxy URL.

        Called from :class:`MainWindow` when the proxy signals it's
        listening (``started_ok``). On first call with a non-empty URL
        the heavy ``DashboardView`` is built and the placeholder label
        is replaced. An empty URL stops both views' polling — the
        dashboard keeps its last-rendered page, the recent activity
        table clears.
        """
        url = url.rstrip("/")
        # Build the dashboard view lazily to avoid the QWebEngineWidgets
        # import until we actually have a proxy to point at.
        if url and self.dashboard_view is None:
            self._build_dashboard_view()
        if self.dashboard_view is not None:
            self.dashboard_view.set_base_url(url)
        if self.recent_activity_view is not None:
            self.recent_activity_view.set_base_url(url)

    def stop_polling(self) -> None:
        """Stop the recent-activity view's polling (dashboard keeps
        its last-rendered page). Idempotent.
        """
        if self.recent_activity_view is not None:
            self.recent_activity_view.stop()

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

        # Resolve the API key for the profile's default model. The
        # helper walks model_endpoints → key_ref → CredentialStore. If
        # the model has no key configured, we still launch with the
        # sentinel (Claude Code refuses to start without an
        # ``ANTHROPIC_API_KEY``) and warn the user.
        api_key = resolve_api_key(profile, self.profile_store.credential_store)
        if api_key is None and profile.default_model:
            endpoint = profile.model_endpoints.get(profile.default_model)
            if endpoint and endpoint.key_ref and not self._suppress_dialogs:
                QMessageBox.warning(
                    self,
                    "API key missing",
                    f"Profile {profile.name!r} references credential "
                    f"{endpoint.key_ref!r} but it has no value stored.\n\n"
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

        No-op if the current inner tab is one of the pinned tabs
        (Dashboard / Recent Activity) — those don't have a text edit
        to focus.
        """
        widget = self.tabs.currentWidget()
        if isinstance(widget, TerminalSessionView):
            widget.focus_terminal()

    def show_dashboard(self) -> None:
        """Switch the inner tab strip to the Dashboard pane."""
        self.tabs.setCurrentIndex(0)

    def show_recent_activity(self) -> None:
        """Switch the inner tab strip to the Recent Activity pane."""
        self.tabs.setCurrentIndex(1)

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def shutdown(self) -> None:
        """Idempotent full-shutdown of every Claude session and the
        embedded polling widgets.
        """
        for view in list(self._sessions):
            try:
                view.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if self.recent_activity_view is not None:
            try:
                self.recent_activity_view.shutdown()
            except Exception:  # noqa: BLE001
                pass

    # ---- internals ---------------------------------------------------------

    def _build_dashboard_view(self) -> None:
        """Replace the placeholder label with the real ``DashboardView``.

        Split out so the heavy ``QWebEngineWidgets`` import and the
        ``replaceWidget`` swap are isolated in one place. Idempotent
        — guards against the second call from a stop/start cycle.
        """
        if self.dashboard_view is not None or self._dashboard_placeholder is None:
            return
        view = DashboardView()
        view.url_loaded.connect(
            lambda url: self.parent().statusBar().showMessage(
                f"Dashboard loaded: {url}", 3000
            )
            if self.parent() is not None and hasattr(self.parent(), "statusBar")
            else None
        )
        old = self._dashboard_placeholder
        self._dashboard_placeholder = None  # type: ignore[assignment]
        self.dashboard_layout.replaceWidget(old, view)
        old.deleteLater()
        self.dashboard_view = view

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
        # Surface the resolved proxy URL so the user can verify
        # Claude Code will route through us. If ``ANTHROPIC_BASE_URL``
        # is empty the session is talking to Anthropic's real API —
        # the most common cause of "HTTP 200 / empty body / no proxy
        # record" reports is a session launched before the proxy was
        # running, or a stale terminal session.
        proxy_url = env.get("ANTHROPIC_BASE_URL", "")
        default_model = env.get("ANTHROPIC_MODEL", "")
        logger.info(
            "launching claude session: profile=%r default_model=%r "
            "ANTHROPIC_BASE_URL=%r",
            profile.name,
            default_model or "(none)",
            proxy_url or "(EMPTY — claude will hit api.anthropic.com)",
        )

        view = TerminalSessionView(
            argv=["claude"],
            env=env,
            cwd=profile.session_cwd,
            session_label="claude",
            parent=self,
        )
        view.exited.connect(lambda v=view: self._on_session_exited(v))
        # Tab title shows the profile so the user can tell two tabs of
        # the same ``claude`` binary apart at a glance.
        title = profile.name
        index = self.tabs.addTab(view, title)
        self.tabs.setCurrentIndex(index)
        self._sessions.append(view)
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

        # --- Inner tab strip ---------------------------------------------
        # A single QTabWidget hosts Dashboard (index 0), Recent Activity
        # (index 1), and any number of Claude sessions (index 2+).
        # ``tabsClosable`` is enabled so session tabs show their ``×``;
        # ``tabCloseRequested`` is filtered to ignore pinned indices.
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        # Movement is disabled so the "Dashboard always first, Recent
        # Activity always second, sessions from index 2 onward" ordering
        # holds across user interaction. The first two tabs are pinned
        # in the literal sense: their close buttons are hidden and the
        # close-request slot refuses to close them.
        self.tabs.setMovable(False)
        self.tabs.tabCloseRequested.connect(self._on_close_requested)

        # Pinned tab 0: Dashboard. The placeholder is replaced by the
        # real ``DashboardView`` lazily, on first proxy start — see
        # ``_build_dashboard_view``.
        self.dashboard_container = QWidget()
        self.dashboard_layout = QVBoxLayout(self.dashboard_container)
        self.dashboard_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_layout.setSpacing(0)
        self._dashboard_placeholder = QLabel(
            "Dashboard not yet available.\n\n"
            "Start the proxy via:\n"
            "    Claude → Start Proxy\n\n"
            "Or configure it to auto-start via:\n"
            "    Claude → Settings..."
        )
        self._dashboard_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dashboard_placeholder.setStyleSheet("color: gray; font-size: 14px;")
        self.dashboard_layout.addWidget(self._dashboard_placeholder)
        self.tabs.addTab(self.dashboard_container, "Dashboard")

        # Pinned tab 1: Recent Activity. Built up-front (no heavy deps);
        # polling stays paused until ``set_base_url`` is called.
        self.recent_activity_view = RecentActivityView()
        self.tabs.addTab(self.recent_activity_view, "Recent Activity")

        # Strip the close buttons from the pinned tabs. ``setTabButton``
        # with ``None`` removes the button that ``setTabsClosable(True)``
        # would otherwise install.
        self._set_pinned_tab(0)
        self._set_pinned_tab(1)

        layout.addWidget(self.tabs, 1)

    def _set_pinned_tab(self, index: int) -> None:
        """Hide the close button on a pinned tab so it can't be closed.

        ``QTabBar.setTabButton(idx, RightSide, None)`` removes the
        button Qt installed automatically. ``tabCloseRequested`` is
        still filtered in ``_on_close_requested`` so any programmatic
        attempt to close a pinned tab is also rejected.
        """
        self.tabs.tabBar().setTabButton(
            index, QTabBar.ButtonPosition.RightSide, None
        )

    def _on_close_requested(self, index: int) -> None:
        """Slot for the inner tab strip's ``tabCloseRequested``.

        The first ``_NUM_PINNED`` tabs are pinned (Dashboard, Recent
        Activity) and cannot be closed by user or by code. Closing
        attempts for those indices are silently ignored.
        """
        if index < _NUM_PINNED:
            return
        self._close_session(index)

    def _on_new_clicked(self) -> None:
        # Toolbar button click → new session with the active profile.
        # The container doesn't decide which profile — that's the main
        # window's job (it owns the dropdown). It calls back into
        # new_session() via _on_new_session_requested.
        self.new_session()

    def _close_session(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is None:
            return
        try:
            widget.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.tabs.removeTab(index)
        if widget in self._sessions:
            self._sessions.remove(widget)
        widget.deleteLater()
        self._update_info_label()

    def _on_session_exited(self, view: TerminalSessionView) -> None:
        # Linear scan — session count is small (handful at most), and
        # the close-button-hidden pinned tabs guarantee that index 0
        # and 1 never match a ``TerminalSessionView``.
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is view:
                # Reuse the view's own title() so the suffix matches
                # what TerminalSessionView already says (e.g. "claude
                # (exited)"). The profile name is shown as the prefix
                # in _launch_session's addTab call; the view's title()
                # returns just the basename. We compromise by keeping
                # the profile name + appending "(exited)" if needed.
                title = view.title()
                if "(exited)" not in title:
                    title = f"{title} (exited)"
                self.tabs.setTabText(i, title)
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
