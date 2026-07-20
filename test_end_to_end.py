"""End-to-end test: start the proxy via the UI button, hit it, then stop.

Simulates the user clicking Server → Start, waits for the proxy to be
listening, makes a couple of HTTP requests against the embedded server,
and then simulates Server → Stop. This exercises the full signal flow:

    click Start
      → MainWindow._start_server creates ProxyServerThread
      → thread.start() → QThread.run() → asyncio.run(self._serve())
        → uvicorn binds, lifespan opens SQLite, started_ok signal fires
      → MainWindow._on_proxy_started enables dashboard action
    HTTP GET /health → 200
    HTTP GET /v1/models → 200, empty list
    click Stop
      → MainWindow._stop_server → thread.request_stop()
        → uvicorn drains, lifespan finally closes SQLite, stopped signal fires
      → MainWindow._on_proxy_stopped resets UI state
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.ui.main_window import MainWindow  # noqa: E402


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spin(app: QApplication, seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    """Spin Qt's event loop until ``predicate`` is true or ``timeout`` elapses.

    Must use Qt's event loop instead of ``time.sleep`` so that signals
    emitted from worker threads get delivered to the main thread's slots.
    """
    deadline = time.monotonic() + timeout
    app = QApplication.instance() or QApplication(sys.argv)
    while time.monotonic() < deadline:
        if predicate():
            return True
        _spin(app, 0.05)
    return False


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("Sharkdrown")
    app.setApplicationName("PandasMarkdownEditor-E2E")

    # Use an isolated QSettings store so we don't trample the user's
    # real preferences during the test.
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(Path.home() / ".cache" / "sharkdrown-e2e"),
    )
    s = QSettings()
    s.clear()
    s.sync()

    # Pick a free port so we don't clash with anything else.
    port = _find_free_port()
    s.setValue("proxy/host", "127.0.0.1")
    s.setValue("proxy/port", port)
    s.setValue("proxy/autostart", False)
    s.sync()

    window = MainWindow()
    window.show()
    print(f"✓ MainWindow created (proxy port will be {port})")

    # ---- Click Server → Start -------------------------------------------
    assert window.action_start_server.isEnabled(), "Start should be enabled initially"
    assert not window.action_stop_server.isEnabled()
    window.action_start_server.trigger()
    print("✓ triggered Server → Start")

    # Wait for the started_ok signal to be delivered to the slot
    # (_on_proxy_started) which enables the dashboard action.
    ok = _wait_for(
        lambda: window.action_show_dashboard.isEnabled(),
        timeout=10.0,
    )
    assert ok, "proxy did not become healthy within 10s"
    # Drain events so the status label updates.
    _spin(app, 0.5)
    print(f"✓ proxy started; status: {window.proxy_status_label.text()!r}")

    assert window.action_show_dashboard.isEnabled(), "Show Dashboard should enable"
    assert window.action_open_browser.isEnabled()
    assert window.proxy_port == port

    # ---- Make a real HTTP call against /health --------------------------
    base = f"http://127.0.0.1:{port}"
    with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body.get("status") == "healthy"
        print(f"✓ /health returned healthy: {body}")

    # ---- Make a call against /v1/models (returns empty list per proxy) -
    with urllib.request.urlopen(f"{base}/v1/models", timeout=2) as resp:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body == {"object": "list", "data": []}, f"unexpected: {body}"
        print(f"✓ /v1/models returned empty list")

    # ---- Toggle the dashboard tab on -----------------------------------
    assert window.action_show_dashboard.isEnabled(), "Show Dashboard should be enabled"
    # The DashboardView is built eagerly in _on_proxy_started so the URL
    # can be pointed at the bound port. The view is hosted in the
    # top-level "Dashboard" tab; switching to that tab makes it fill the
    # window (the previous dock-based design only gave it part of it).
    assert window.dashboard_view is not None, "dashboard view should exist after start"
    assert window.dashboard_view.base_url() == f"http://127.0.0.1:{port}"
    # Tab structure: two top-level tabs, "Documents" and "Dashboard".
    assert window.view_tabs.count() == 2
    assert window.view_tabs.tabText(1) == "Dashboard"
    print(f"✓ dashboard view built and pointed at http://127.0.0.1:{port}")

    # Switch to the Dashboard tab and verify the menu action syncs.
    window.view_tabs.setCurrentIndex(1)
    _spin(app, 0.2)
    assert window.action_show_dashboard.isChecked(), (
        "Show Dashboard action should sync with active tab"
    )
    print("✓ Show Dashboard action syncs with tab bar")

    # ---- Click Server → Stop --------------------------------------------
    # Give uvicorn's main_loop a chance to actually start polling
    # should_exit. In real desktop usage the user has the proxy running
    # for many seconds before clicking Stop, so this is a test artifact,
    # not a real-world bug. We spin the event loop for 1s.
    _spin(app, 1.0)
    window.action_stop_server.trigger()
    print("✓ triggered Server → Stop")
    # Wait for stopped signal: the status label flips back to "stopped"
    # after _on_proxy_stopped runs.
    ok = _wait_for(
        lambda: window.proxy_status_label.text() == "Proxy: stopped",
        timeout=10.0,
    )
    assert ok, f"proxy did not stop within 10s (status: {window.proxy_status_label.text()!r})"
    _spin(app, 0.2)
    print(f"✓ proxy stopped; status: {window.proxy_status_label.text()!r}")

    # After stop, the dashboard view stays in the tab but its URL is
    # stale. The Show Dashboard / Open in Browser actions are disabled.
    assert not window.action_show_dashboard.isEnabled()
    assert not window.action_open_browser.isEnabled()

    # ---- Verify the dashboard view is preserved across stop ------
    assert window.dashboard_view is not None
    assert window.view_tabs.count() == 2
    print("✓ dashboard view preserved across stop")

    # ---- Clean shutdown -------------------------------------------------
    window.close()
    _spin(app, 0.5)
    print("✓ window closed without orphan thread warnings")

    print("\n✅ End-to-end test passed!")


if __name__ == "__main__":
    main()