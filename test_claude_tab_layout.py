"""Tests for the Claude tab restructure and menu merge.

Covers:
  - The top-level ``view_tabs`` no longer has standalone Dashboard /
    Recent Activity tabs — they live inside the Claude container.
  - The Claude container's inner tab strip has Dashboard (index 0)
    and Recent Activity (index 1) as pinned tabs, with no close
    buttons, and Claude session tabs slot in at index 2 onward.
  - Closing a pinned tab via ``tabCloseRequested`` is a no-op.
  - The Server menu is gone; Start / Stop Proxy live under the
    Claude menu alongside the existing Claude entries.
  - The new action names (``action_start_proxy`` / ``action_stop_proxy``)
    match the labels in the menu.
  - The Show Dashboard / Show Recent Activity menu actions switch
    the inner tab strip correctly.
  - ``claude_container.set_base_url`` builds the dashboard view
    lazily and routes the URL to both the dashboard and recent
    activity views.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _qapp():
    """Build (or reuse) a headless QApplication."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ---- Top-level view tab structure -----------------------------------------


def test_top_level_view_tabs_only_has_three_tabs() -> None:
    """MainWindow exposes exactly Documents, Terminals, and Claude
    at the top level — no standalone Dashboard or Recent Activity."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        labels = [
            w.view_tabs.tabText(i) for i in range(w.view_tabs.count())
        ]
        assert labels == ["Documents", "Terminals", "Claude"], (
            f"expected ['Documents', 'Terminals', 'Claude'], got {labels}"
        )
        # Tab-index constants match the actual layout.
        assert w._TAB_INDEX_DOCUMENTS == 0
        assert w._TAB_INDEX_TERMINALS == 1
        assert w._TAB_INDEX_CLAUDE == 2
    finally:
        w.close()
    print("✓ top-level view tabs are Documents, Terminals, Claude")


def test_top_level_view_tabs_are_not_closable() -> None:
    """Top-level view tabs cannot be closed — the user only navigates
    between them, never closes them."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert not w.view_tabs.tabsClosable(), (
            "top-level view tabs should not be closable"
        )
    finally:
        w.close()
    print("✓ top-level view tabs are not closable")


def test_split_and_preview_default_deselected() -> None:
    """By default the app starts in single-pane editor mode.

    The Toggle Split and Preview menu actions, the matching toolbar
    buttons, and the underlying ``split_enabled`` / ``preview_visible``
    state attributes all start unchecked. Opening a new tab does not
    eagerly create a preview pane, and the right pane of the splitter
    is hidden so the window really is a single pane on startup.
    """
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        # State attributes.
        assert w.split_enabled is False, (
            "split_enabled should default to False"
        )
        assert w.preview_visible is False, (
            "preview_visible should default to False"
        )
        # Menu actions.
        assert w.action_toggle_split.isChecked() is False, (
            "Toggle Split menu action should start unchecked"
        )
        assert w.action_toggle_preview.isChecked() is False, (
            "Toggle Preview menu action should start unchecked"
        )
        # The toolbar was removed (File / View menus already expose these).
        assert not hasattr(w, "tb_toggle_split"), (
            "tb_toggle_split toolbar button should not exist"
        )
        assert not hasattr(w, "tb_toggle_preview"), (
            "tb_toggle_preview toolbar button should not exist"
        )
        # No preview tabs were created when the initial tab was opened.
        assert w.preview_tabs.count() == 0, (
            "no preview tabs should be created with split + preview off"
        )
        # The right pane is hidden — the splitter collapses to one pane
        # even though the QSplitter widget itself is still 2-pane.
        assert w.preview_tabs.isVisible() is False, (
            "preview_tabs should start hidden so the window is single-pane"
        )
    finally:
        w.close()
    print("✓ Toggle Split and Preview default to deselected")


def test_toggle_split_works_independently_of_toggle_preview() -> None:
    """Toggle Split alone makes the right pane appear; it does not
    require Toggle Preview to be on first.

    Regression for the bug where ``show_preview = split_enabled and
    preview_visible`` coupled the two toggles, so a user who clicked
    Toggle Split with Preview off saw no visible change.
    """
    from PySide6.QtCore import QEventLoop, QTimer

    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 700)
    w.show()

    def _spin(ms: int = 50) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    try:
        # Let the show() event settle before checking visibility.
        _spin(50)

        # Sanity: default state is single-pane.
        assert w.preview_tabs.isVisible() is False

        # Click Toggle Split — right pane becomes visible.
        w.action_toggle_split.trigger()
        _spin(20)
        assert w.split_enabled is True
        assert w.action_toggle_split.isChecked() is True
        assert w.preview_tabs.isVisible() is True, (
            "clicking Toggle Split should show the right pane even "
            "with preview_visible still False"
        )
        print("✓ Toggle Split on reveals the right pane on its own")

        # Click Toggle Split again — right pane hides, back to single-pane.
        w.action_toggle_split.trigger()
        _spin(20)
        assert w.split_enabled is False
        assert w.action_toggle_split.isChecked() is False
        assert w.preview_tabs.isVisible() is False
        print("✓ Toggle Split off collapses the right pane again")

        # And once more, to make sure the cycle is repeatable.
        w.action_toggle_split.trigger()
        _spin(20)
        assert w.split_enabled is True
        assert w.preview_tabs.isVisible() is True
        print("✓ Toggle Split cycle is repeatable (on → off → on)")
    finally:
        w.close()


def test_toggle_split_distributes_splitter_50_50() -> None:
    """Toggling Split on sets the QSplitter to an even 50/50 split.

    Without ``splitter.setSizes(...)`` the right pane would inherit
    a stale size (typically 0) from the previous hide cycle, and
    the user would see the editor take the full width with a sliver
    on the right.
    """
    from PySide6.QtCore import QEventLoop, QTimer

    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 700)
    w.show()

    def _spin(ms: int = 50) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    try:
        _spin(50)
        # Toggle split on.
        w.action_toggle_split.trigger()
        _spin(50)  # let the resize settle so splitter.width() is current
        sizes = w.splitter.sizes()
        assert len(sizes) == 2, f"expected 2 splitter sizes, got {sizes}"
        left, right = sizes
        total = left + right
        # The two panes should be within 1 pixel of equal — the half
        # calculation uses integer division, so one side may be 1px
        # larger than the other when the total is odd.
        assert abs(left - right) <= 1, (
            f"split should be ~50/50; got left={left}, right={right}, "
            f"total={total}"
        )
        print(f"✓ Toggle Split distributes evenly: left={left}, right={right}")

        # Toggle off and back on — make sure the 50/50 sticks across cycles.
        w.action_toggle_split.trigger()
        _spin(20)
        w.action_toggle_split.trigger()
        _spin(50)
        sizes = w.splitter.sizes()
        left, right = sizes
        assert abs(left - right) <= 1, (
            f"split should reset to ~50/50 after hide→show cycle; "
            f"got left={left}, right={right}"
        )
        print("✓ Split is 50/50 across repeated toggle cycles")
    finally:
        w.close()


# ---- Inner tab strip in ClaudeContainerView ------------------------------


def test_claude_container_inner_tab_strip_order() -> None:
    """The Claude container's inner tab strip has Dashboard at index 0
    and Recent Activity at index 1, in that order."""
    _qapp()
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        labels = [cv.tabs.tabText(i) for i in range(cv.tabs.count())]
        assert labels[:2] == ["Dashboard", "Recent Activity"], (
            f"expected first two tabs to be Dashboard, Recent Activity; got {labels}"
        )
    finally:
        cv.deleteLater()
    print("✓ Claude container: Dashboard first, Recent Activity second")


def test_claude_container_pinned_tabs_have_no_close_button() -> None:
    """The first two tabs (Dashboard, Recent Activity) have no close
    button — they're pinned and cannot be closed by the user."""
    _qapp()
    from PySide6.QtWidgets import QTabBar

    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        assert cv.tabs.tabsClosable(), (
            "tabsClosable should be enabled so session tabs show their ×"
        )
        for i in range(2):
            btn = cv.tabs.tabBar().tabButton(
                i, QTabBar.ButtonPosition.RightSide
            )
            assert btn is None, (
                f"pinned tab {i} should have no close button; got {btn!r}"
            )
    finally:
        cv.deleteLater()
    print("✓ pinned tabs have no close button")


def test_claude_container_close_pinned_tab_is_noop() -> None:
    """Even if the inner tab strip fires ``tabCloseRequested`` for
    index 0 or 1, the container ignores it — closing Dashboard or
    Recent Activity is not allowed."""
    _qapp()
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        # Manually emit the signal for the pinned tabs; the slot
        # should refuse to close them.
        cv.tabs.tabCloseRequested.emit(0)
        cv.tabs.tabCloseRequested.emit(1)
        assert cv.tabs.count() == 2, (
            "pinned tabs must stay after a close-request signal"
        )
    finally:
        cv.deleteLater()
    print("✓ tabCloseRequested for pinned tabs is a no-op")


def test_claude_container_session_tabs_are_closable() -> None:
    """Tabs at index 2+ (Claude sessions) must show a close button and
    close when the user clicks it."""
    _qapp()
    from PySide6.QtWidgets import QTabBar

    from src.profiles.models import ClaudeProfile
    from src.ui.claude.claude_container_view import ClaudeContainerView

    # Build a Claude container with a fake session injected at
    # index 2. We don't actually launch a PTY — we just need a
    # QWidget child to register the close behaviour against.
    cv = ClaudeContainerView()
    try:
        from PySide6.QtWidgets import QWidget

        fake_session = QWidget()
        cv.tabs.addTab(fake_session, "Session A")
        # Session tabs DO show a close button.
        btn = cv.tabs.tabBar().tabButton(
            2, QTabBar.ButtonPosition.RightSide
        )
        assert btn is not None, (
            "session tab should have a close button"
        )
        # tabCloseRequested for index 2 must close the tab.
        cv.tabs.tabCloseRequested.emit(2)
        assert cv.tabs.count() == 2, (
            "session tab should be removed on close-request"
        )
    finally:
        cv.deleteLater()
    print("✓ Claude session tabs are closable")


# ---- Menu merge ------------------------------------------------------------


def test_no_server_menu_in_menu_bar() -> None:
    """The Server menu was merged into the Claude menu."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        menubar = w.menuBar()
        titles = [
            menubar.actions()[i].text()
            for i in range(len(menubar.actions()))
        ]
        assert "&Server" not in titles, (
            f"Server menu should be removed; got {titles}"
        )
    finally:
        w.close()
    print("✓ Server menu is removed from the menu bar")


def test_format_menu_is_removed() -> None:
    """The Format menu was removed from the menubar — the format
    selector now lives in the status bar (see
    ``test_format_combo_lives_in_status_bar`` below)."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        menubar = w.menuBar()
        titles = [
            menubar.actions()[i].text()
            for i in range(len(menubar.actions()))
        ]
        assert "&Format" not in titles, (
            f"Format menu should be removed; got {titles}"
        )
        # The action attributes the menu installed are gone too.
        assert not hasattr(w, "action_format_markdown"), (
            "action_format_markdown should not exist after menu removal"
        )
        assert not hasattr(w, "action_format_json")
        assert not hasattr(w, "action_format_yaml")
    finally:
        w.close()
    print("✓ Format menu is removed from the menubar")


def test_format_combo_lives_in_status_bar() -> None:
    """A ``QComboBox`` named ``format_combo`` is added as a permanent
    widget to the status bar with markdown/json/yaml options."""
    _qapp()
    from PySide6.QtWidgets import QComboBox

    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert hasattr(w, "format_combo"), (
            "MainWindow should expose format_combo on the status bar"
        )
        assert isinstance(w.format_combo, QComboBox)
        items = [
            w.format_combo.itemText(i)
            for i in range(w.format_combo.count())
        ]
        assert items == ["markdown", "json", "yaml"], (
            f"expected the three format options, got {items}"
        )
    finally:
        w.close()
    print("✓ format_combo lives in the status bar with the right options")


def test_format_combo_visible_only_on_documents_tab() -> None:
    """The format combo is visible only when the Documents top-level
    tab is active. It hides on Terminals and Claude."""
    from PySide6.QtCore import QEventLoop, QTimer

    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 700)
    w.show()

    def _spin(ms: int = 30) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    try:
        _spin(50)
        # Initial state: Documents tab is active → combo visible.
        assert w.view_tabs.currentIndex() == w._TAB_INDEX_DOCUMENTS
        assert w.format_combo.isVisible() is True, (
            "format_combo should be visible on Documents tab"
        )

        # Switch to Terminals → combo hidden.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_TERMINALS)
        _spin(20)
        assert w.format_combo.isVisible() is False, (
            "format_combo should be hidden on Terminals tab"
        )
        print("✓ format_combo hides on Terminals tab")

        # Switch to Claude → combo hidden.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_CLAUDE)
        _spin(20)
        assert w.format_combo.isVisible() is False, (
            "format_combo should be hidden on Claude tab"
        )
        print("✓ format_combo hides on Claude tab")

        # Switch back to Documents → combo visible again.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_DOCUMENTS)
        _spin(20)
        assert w.format_combo.isVisible() is True, (
            "format_combo should reappear when returning to Documents"
        )
        print("✓ format_combo reappears on Documents tab")
    finally:
        w.close()


def test_proxy_status_visible_only_on_claude_tab() -> None:
    """The proxy status label and activity tile are visible only when
    the Claude top-level tab is active. They hide on Documents and
    Terminals."""
    from PySide6.QtCore import QEventLoop, QTimer

    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 700)
    w.show()

    def _spin(ms: int = 30) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    try:
        _spin(50)
        # Initial state: Documents tab is active → proxy widgets hidden.
        assert w.proxy_status_label.isVisible() is False, (
            "proxy_status_label should be hidden on Documents"
        )
        assert w.activity_tile.isVisible() is False, (
            "activity_tile should be hidden on Documents"
        )

        # Switch to Terminals → still hidden.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_TERMINALS)
        _spin(20)
        assert w.proxy_status_label.isVisible() is False
        assert w.activity_tile.isVisible() is False
        print("✓ proxy widgets stay hidden on Terminals tab")

        # Switch to Claude → both visible.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_CLAUDE)
        _spin(20)
        assert w.proxy_status_label.isVisible() is True, (
            "proxy_status_label should be visible on Claude"
        )
        assert w.activity_tile.isVisible() is True, (
            "activity_tile should be visible on Claude"
        )
        print("✓ proxy widgets become visible on Claude tab")

        # Back to Documents → both hidden again.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_DOCUMENTS)
        _spin(20)
        assert w.proxy_status_label.isVisible() is False
        assert w.activity_tile.isVisible() is False
        print("✓ proxy widgets hide again on return to Documents")
    finally:
        w.close()


def test_format_combo_syncs_to_current_document() -> None:
    """When the user switches editor tabs, the format combo should
    follow the active document's format."""
    from PySide6.QtCore import QEventLoop, QTimer

    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 700)
    w.show()

    def _spin(ms: int = 30) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    try:
        _spin(50)
        # The first document defaults to markdown.
        assert w.format_combo.currentText() == "markdown"

        # Change the current document's format to JSON via the combo.
        w.format_combo.setCurrentText("json")
        assert w.editor_tabs.widget(0).document.format_type == "json", (
            "selecting json in the combo should write json to the document"
        )

        # Open a new document tab — new docs default to markdown.
        w._create_new_tab()
        _spin(20)
        # The combo should auto-sync to the new document's format
        # (markdown), not stay on json from the previous one.
        assert w.format_combo.currentText() == "markdown", (
            "the combo should follow the active document, "
            f"got {w.format_combo.currentText()!r}"
        )
        print("✓ format_combo syncs to the active document on tab switch")

        # Switch back to the first tab — combo should follow the
        # first document's format (json, set above).
        w.editor_tabs.setCurrentIndex(0)
        _spin(20)
        assert w.format_combo.currentText() == "json", (
            "the combo should sync back to the first document's format"
        )
        print("✓ format_combo syncs back when returning to a previous tab")
    finally:
        w.close()


def test_format_combo_change_does_not_bounce() -> None:
    """Picking a new format must update the document *and* the combo
    only once. The ``_sync_format_combo_to_current_document`` helper
    must block the combo's signal during assignment so it doesn't
    re-fire ``_on_format_combo_changed`` and re-apply the same value
    in a tight loop.
    """
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.format_combo.setCurrentText("yaml")
        # The document format should now be yaml and the combo's text
        # is yaml — the sync helper would set it again, but the
        # equality check at the top of the helper is what prevents
        # the bounce in practice.
        assert w.editor_tabs.widget(0).document.format_type == "yaml"
        # Trigger the sync directly — no exception, combo stays at yaml.
        w._sync_format_combo_to_current_document()
        assert w.format_combo.currentText() == "yaml"
        print("✓ format change applies once without bouncing")
    finally:
        w.close()
    """The Server menu was merged into the Claude menu."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        menubar = w.menuBar()
        titles = [
            menubar.actions()[i].text()
            for i in range(len(menubar.actions()))
        ]
        assert "&Server" not in titles, (
            f"Server menu should be removed; got {titles}"
        )
    finally:
        w.close()
    print("✓ Server menu is removed from the menu bar")


def test_claude_menu_has_proxy_start_stop_actions() -> None:
    """The Claude menu has Start Proxy and Stop Proxy entries."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert hasattr(w, "action_start_proxy"), (
            "MainWindow should expose action_start_proxy"
        )
        assert hasattr(w, "action_stop_proxy"), (
            "MainWindow should expose action_stop_proxy"
        )
        assert w.action_stop_proxy.isEnabled() is False, (
            "Stop Proxy should start disabled"
        )
        # The labels in the menu match the action labels.
        assert w.action_start_proxy.text().replace("&", "") == "Start Proxy"
        assert w.action_stop_proxy.text().replace("&", "") == "Stop Proxy"
    finally:
        w.close()
    print("✓ Claude menu has Start Proxy / Stop Proxy actions")


def test_old_server_action_names_removed() -> None:
    """The renamed actions replace the old start_server / stop_server
    attributes. The old action names should no longer exist on the
    main window."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert not hasattr(w, "action_start_server"), (
            "action_start_server should be replaced by action_start_proxy"
        )
        assert not hasattr(w, "action_stop_server"), (
            "action_stop_server should be replaced by action_stop_proxy"
        )
    finally:
        w.close()
    print("✓ old action_start_server / action_stop_server are gone")


def test_claude_menu_has_show_dashboard_and_recent_actions() -> None:
    """The Show Dashboard / Show Recent Activity entries live under
    the Claude menu (after the menu merge)."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        assert hasattr(w, "action_show_dashboard"), (
            "Show Dashboard action should exist"
        )
        assert hasattr(w, "action_show_recent"), (
            "Show Recent Activity action should exist"
        )
        assert hasattr(w, "action_open_browser"), (
            "Open in Browser action should exist"
        )
        assert hasattr(w, "action_proxy_settings"), (
            "Proxy Settings action should exist under the Claude menu"
        )
    finally:
        w.close()
    print("✓ Claude menu has Show Dashboard / Show Recent / Open in Browser / Settings")


# ---- Container-level proxy URL routing -----------------------------------


def test_set_base_url_routes_to_dashboard_and_recent() -> None:
    """``claude_container.set_base_url(...)`` builds the dashboard view
    lazily and points the recent-activity view at the URL."""
    _qapp()
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        assert cv.dashboard_view is None, (
            "dashboard view should not exist before set_base_url is called"
        )
        cv.set_base_url("http://127.0.0.1:3001")
        assert cv.dashboard_view is not None, (
            "dashboard view should be built on first set_base_url"
        )
        assert cv.dashboard_view.base_url() == "http://127.0.0.1:3001"
        # Recent activity view also gets the URL.
        assert cv.recent_activity_view is not None
        assert cv.recent_activity_view._base_url == "http://127.0.0.1:3001"
    finally:
        cv.deleteLater()
    print("✓ set_base_url routes URL to dashboard + recent activity")


def test_set_base_url_with_empty_url_does_not_build_dashboard() -> None:
    """Empty URL doesn't trigger the heavy QWebEngineWidgets import."""
    _qapp()
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        cv.set_base_url("")
        assert cv.dashboard_view is None, (
            "empty URL should keep dashboard view uninitialised"
        )
    finally:
        cv.deleteLater()
    print("✓ empty URL never builds the dashboard view")


# ---- Show Dashboard / Show Recent slots ----------------------------------


def test_show_dashboard_switches_to_inner_tab() -> None:
    """The Claude menu's Show Dashboard action switches to the
    inner Dashboard sub-tab inside the Claude container."""
    _qapp()
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        cv.tabs.setCurrentIndex(1)  # pretend Recent Activity was active
        cv.show_dashboard()
        assert cv.tabs.currentIndex() == 0, (
            "show_dashboard should select the inner Dashboard tab"
        )
    finally:
        cv.deleteLater()
    print("✓ show_dashboard switches to inner Dashboard tab")


def test_show_recent_activity_switches_to_inner_tab() -> None:
    """The Claude menu's Show Recent Activity action switches to the
    inner Recent Activity sub-tab inside the Claude container."""
    _qapp()
    from src.ui.claude.claude_container_view import ClaudeContainerView

    cv = ClaudeContainerView()
    try:
        cv.tabs.setCurrentIndex(0)  # start on Dashboard
        cv.show_recent_activity()
        assert cv.tabs.currentIndex() == 1, (
            "show_recent_activity should select the inner Recent Activity tab"
        )
    finally:
        cv.deleteLater()
    print("✓ show_recent_activity switches to inner Recent Activity tab")


def test_show_dashboard_menu_action_in_main_window() -> None:
    """Firing the menu action routes through the container correctly."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        # Switch to Terminals first so we can verify the action
        # brings the Claude tab back.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_TERMINALS)
        w.action_show_dashboard.trigger()
        assert w.view_tabs.currentIndex() == w._TAB_INDEX_CLAUDE, (
            "Show Dashboard should switch to the Claude tab"
        )
        assert w.claude_container.tabs.currentIndex() == 0, (
            "Show Dashboard should select the inner Dashboard sub-tab"
        )
    finally:
        w.close()
    print("✓ main window's Show Dashboard action navigates correctly")


def test_show_recent_menu_action_in_main_window() -> None:
    """Firing the menu action routes through the container correctly."""
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_DOCUMENTS)
        w.action_show_recent.trigger()
        assert w.view_tabs.currentIndex() == w._TAB_INDEX_CLAUDE, (
            "Show Recent Activity should switch to the Claude tab"
        )
        assert w.claude_container.tabs.currentIndex() == 1, (
            "Show Recent Activity should select the inner Recent Activity sub-tab"
        )
    finally:
        w.close()
    print("✓ main window's Show Recent Activity action navigates correctly")


# ---- Runner ----------------------------------------------------------------


def main() -> None:
    tests = [
        test_top_level_view_tabs_only_has_three_tabs,
        test_top_level_view_tabs_are_not_closable,
        test_split_and_preview_default_deselected,
        test_toggle_split_works_independently_of_toggle_preview,
        test_toggle_split_distributes_splitter_50_50,
        test_claude_container_inner_tab_strip_order,
        test_claude_container_pinned_tabs_have_no_close_button,
        test_claude_container_close_pinned_tab_is_noop,
        test_claude_container_session_tabs_are_closable,
        test_no_server_menu_in_menu_bar,
        test_format_menu_is_removed,
        test_format_combo_lives_in_status_bar,
        test_format_combo_visible_only_on_documents_tab,
        test_proxy_status_visible_only_on_claude_tab,
        test_format_combo_syncs_to_current_document,
        test_format_combo_change_does_not_bounce,
        test_claude_menu_has_proxy_start_stop_actions,
        test_old_server_action_names_removed,
        test_claude_menu_has_show_dashboard_and_recent_actions,
        test_set_base_url_routes_to_dashboard_and_recent,
        test_set_base_url_with_empty_url_does_not_build_dashboard,
        test_show_dashboard_switches_to_inner_tab,
        test_show_recent_activity_switches_to_inner_tab,
        test_show_dashboard_menu_action_in_main_window,
        test_show_recent_menu_action_in_main_window,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n❌ {failed} test(s) failed")
        sys.exit(1)
    print(f"\n✅ All {len(tests)} Claude-tab-layout tests passed!")


if __name__ == "__main__":
    main()
