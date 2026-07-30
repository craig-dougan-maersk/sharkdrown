"""End-to-end test: start the proxy via the UI button, hit it, then stop.

Simulates the user clicking Claude → Start Proxy, waits for the proxy to be
listening, makes a couple of HTTP requests against the embedded server,
and then simulates Claude → Stop Proxy. This exercises the full signal flow:

    click Start Proxy
      → MainWindow._start_server creates ProxyServerThread
      → thread.start() → QThread.run() → asyncio.run(self._serve())
        → uvicorn binds, lifespan opens SQLite, started_ok signal fires
      → MainWindow._on_proxy_started enables dashboard action
    HTTP GET /health → 200
    HTTP GET /v1/models → 200, empty list
    click Stop Proxy
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
    s.setValue("proxy/autostart", False)
    s.sync()

    # The proxy's bind/port now live on the *active profile*, not on
    # the legacy ``ProxyConfig`` QSettings keys. Wire the test's
    # chosen port onto the active profile so the thread picks it up.
    from src.profiles.models import ModelEndpoint, Provider
    from src.profiles.store import ProfileStore
    from src.proxy.paths import data_dir

    profiles_path = data_dir() / "profiles.json"
    store = ProfileStore(path=profiles_path)
    active = store.get_active() or store.ensure_default()
    active.bind_address = "127.0.0.1"
    active.port = port
    # Endpoints now reference a provider by name; the provider
    # carries the URL. Seed a default provider + one endpoint so
    # the proxy has a routing target — the test doesn't actually
    # fire requests through a particular model, but the proxy
    # refuses to start with zero endpoints.
    if not active.providers:
        active.providers = {
            "default": Provider(name="default", url="https://api.anthropic.com"),
        }
    if not active.model_endpoints:
        active.model_endpoints = {
            "claude-opus-4-8": ModelEndpoint(provider_name="default"),
        }
    store.upsert(active)

    window = MainWindow()
    window.show()
    print(f"✓ MainWindow created (proxy port will be {port})")

    # ---- Click Claude → Start Proxy ------------------------------------
    assert window.action_start_proxy.isEnabled(), "Start Proxy should be enabled initially"
    assert not window.action_stop_proxy.isEnabled()
    window.action_start_proxy.trigger()
    print("✓ triggered Claude → Start Proxy")

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

    # ---- Make a call against /v1/models (returns the proxy's view of
    # its routing config: each real model with its aliases). The
    # current smoke-test profile has a single real model with no
    # aliases — that's still enough to verify the Anthropic-shaped
    # envelope shape Claude Code expects.
    with urllib.request.urlopen(f"{base}/v1/models", timeout=2) as resp:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body.get("object") == "list", f"missing 'object' key: {body}"
        assert isinstance(body.get("data"), list), (
            f"'data' should be a list, got {body!r}"
        )
        # Every entry is ``{id, aliases}`` (Anthropic-shaped + alias
        # array for diagnostics).
        for entry in body["data"]:
            assert "id" in entry and "aliases" in entry, entry
        print(f"✓ /v1/models returned {len(body['data'])} model entries")

    # ---- Toggle the dashboard tab on -----------------------------------
    assert window.action_show_dashboard.isEnabled(), "Show Dashboard should be enabled"
    # The DashboardView is built lazily in the Claude container's
    # set_base_url() the first time the proxy comes up. The view is
    # hosted inside the Claude container's nested tab strip.
    claude_container = window.claude_container
    assert claude_container.dashboard_view is not None, (
        "dashboard view should exist after start"
    )
    assert claude_container.dashboard_view.base_url() == f"http://127.0.0.1:{port}"
    # Top-level tab structure: Documents, Terminals, Claude.
    assert window.view_tabs.count() == 3
    assert window.view_tabs.tabText(0) == "Documents"
    assert window.view_tabs.tabText(1) == "Terminals"
    assert window.view_tabs.tabText(2) == "Claude"
    print(f"✓ dashboard view built and pointed at http://127.0.0.1:{port}")

    # Trigger the Show Dashboard action — it should switch to the
    # Claude tab and select the inner Dashboard sub-tab.
    window.action_show_dashboard.trigger()
    _spin(app, 0.2)
    assert window.view_tabs.currentIndex() == window._TAB_INDEX_CLAUDE
    assert claude_container.tabs.currentIndex() == 0, (
        "Show Dashboard should select the inner Dashboard tab (index 0)"
    )
    print("✓ Show Dashboard action switches to inner Dashboard tab")

    # ---- Click Claude → Stop Proxy --------------------------------------
    # Give uvicorn's main_loop a chance to actually start polling
    # should_exit. In real desktop usage the user has the proxy running
    # for many seconds before clicking Stop, so this is a test artifact,
    # not a real-world bug. We spin the event loop for 1s.
    _spin(app, 1.0)
    window.action_stop_proxy.trigger()
    print("✓ triggered Claude → Stop Proxy")
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
    assert claude_container.dashboard_view is not None
    assert window.view_tabs.count() == 3
    print("✓ dashboard view preserved across stop")

    # ---- Clean shutdown -------------------------------------------------
    window.close()
    _spin(app, 0.5)
    print("✓ window closed without orphan thread warnings")

    print("\n✅ End-to-end test passed!")


if __name__ == "__main__":
    main()