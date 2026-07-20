"""Integration test for the embedded Claude Code proxy.

Boots ``ProxyServerThread`` against a temporary SQLite file, polls ``/health``
until 200, and then asks the thread to shut down. Headless-safe via
``QT_QPA_PLATFORM=offscreen``.

Run with:
    QT_QPA_PLATFORM=offscreen python test_proxy_integration.py
or:
    QT_QPA_PLATFORM=offscreen pytest test_proxy_integration.py -v
"""

from __future__ import annotations

import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

# Headless platform MUST be set before any QWidget is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Add src/ to sys.path so `from src.proxy...` resolves when run directly.
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.proxy.server_thread import ProxyServerThread  # noqa: E402


def _find_free_port() -> int:
    """Ask the OS for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, timeout: float = 10.0) -> bool:
    """Poll ``url`` until 200 OK or ``timeout`` seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — connection refused is expected initially
            time.sleep(0.1)
    return False


def _process_events_for(app: QCoreApplication, seconds: float) -> None:
    """Spin Qt's event loop for ``seconds`` so QThread signals get delivered."""
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def test_proxy_thread_lifecycle(tmp_db: Path, host: str, port: int) -> None:
    """Boot the proxy in a QThread, hit /health, then stop cleanly."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("Sharkdrown")
    app.setApplicationName("PandasMarkdownEditor-Test")

    thread = ProxyServerThread(host=host, port=port, storage_path=tmp_db)
    started_ok: list[int] = []
    failed: list[str] = []
    stopped: list[bool] = []

    thread.started_ok.connect(started_ok.append)
    thread.failed.connect(failed.append)
    thread.stopped.connect(lambda: stopped.append(True))

    thread.start()
    # Give the asyncio loop time to construct the app and bind the socket.
    # We poll /health directly rather than relying solely on started_ok
    # because the latter doesn't fire until uvicorn's serve() begins its
    # accept loop, and /health might respond slightly before that.
    url = f"http://{host}:{port}/health"
    assert _wait_for_health(url, timeout=10.0), (
        f"proxy never responded on {url}; started_ok={started_ok} failed={failed}"
    )
    print(f"✓ proxy is healthy on {url}")

    # Verify SQLite was created by the lifespan.
    assert tmp_db.exists(), f"SQLite database {tmp_db} was not created"
    print(f"✓ SQLite database created at {tmp_db}")

    # Stop the thread.
    thread.request_stop()
    # Drain up to 5 seconds for the lifespan's finally block to run.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not stopped:
        _process_events_for(app, 0.1)
    assert stopped, "proxy thread did not emit stopped signal within 5s"
    assert thread.wait(2000), "proxy thread did not finish within 2s"
    print("✓ proxy stopped cleanly")


def main() -> None:
    """Run the integration test."""
    import tempfile

    port = _find_free_port()
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "test-requests.db"
        test_proxy_thread_lifecycle(tmp_db, host="127.0.0.1", port=port)

    print("\n✅ Proxy integration test passed!")


if __name__ == "__main__":
    main()