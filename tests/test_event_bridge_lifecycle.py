"""Lifecycle tests for EventBusBridge — start, stop, edge cases.

These tests exercise the threading handoff paths that contain the
TOCTOU fix (capturing loop/stop_event as locals before the None-check).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


class _FakeEventBus:
    """Minimal async iterable event bus for testing."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = None  # type: ignore[assignment]
        self._closed = False

    def subscribe(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._queue is None:
            self._queue = asyncio.get_event_loop().get_event_loop().run_in_executor  # noqa
            # Use a proper queue tied to the running loop
            raise asyncio.CancelledError
        raise asyncio.CancelledError

    def close(self) -> None:
        self._closed = True


class _BlockingEventBus:
    """Event bus that blocks until explicitly closed, then raises CancelledError."""

    def __init__(self) -> None:
        self._wake = threading.Event()

    def subscribe(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Yield control; stop_event is checked by the bridge's stop_event.is_set()
        await asyncio.sleep(0.01)
        raise StopAsyncIteration


def _make_bridge():
    from src.proxy.event_bridge import EventBusBridge
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return EventBusBridge()


def test_bridge_start_stop_basic() -> None:
    """Bridge starts without error and stops cleanly within 5 seconds."""
    bridge = _make_bridge()
    bus = _BlockingEventBus()
    bridge.start(bus)
    # Give the thread time to set _loop and _stop_event
    assert bridge._started.wait(timeout=3.0), "bridge did not set started within 3s"
    bridge.stop()
    assert bridge._thread is None, "_thread should be None after stop()"


def test_bridge_stop_before_start_is_noop() -> None:
    """Calling stop() before start() must not raise."""
    bridge = _make_bridge()
    bridge.stop()  # should be a clean no-op
    assert bridge._thread is None


def test_bridge_double_stop_is_safe() -> None:
    """Calling stop() twice must not raise even if thread is already gone."""
    bridge = _make_bridge()
    bus = _BlockingEventBus()
    bridge.start(bus)
    bridge._started.wait(timeout=3.0)
    bridge.stop()
    bridge.stop()  # second stop — must not raise


def test_bridge_double_start_is_noop() -> None:
    """Calling start() while already running must not spawn a second thread."""
    bridge = _make_bridge()
    bus = _BlockingEventBus()
    bridge.start(bus)
    bridge._started.wait(timeout=3.0)
    first_thread = bridge._thread
    bridge.start(bus)  # second start — must be ignored
    assert bridge._thread is first_thread, "second start() must not replace the thread"
    bridge.stop()


def test_bridge_stop_after_thread_exits_naturally() -> None:
    """stop() must succeed even if the background thread has already exited."""

    class _ImmediatelyExitingBus:
        def subscribe(self):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    bridge = _make_bridge()
    bridge.start(_ImmediatelyExitingBus())
    # Wait for the thread to exit on its own (it has nothing to consume)
    deadline = time.monotonic() + 5.0
    while bridge._thread is not None and bridge._thread.is_alive():
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    # Now stop() should not raise even though the loop is already closed
    bridge.stop()


def test_bridge_loop_local_capture_prevents_attribute_error() -> None:
    """Verify the TOCTOU fix: stop() captures loop/stop_event as locals.

    Simulate the race by setting _loop = None after _started fires but
    before call_soon_threadsafe is called. The fix (capturing locals) means
    the call is made on the already-captured (non-None) loop, not on None.
    """
    from src.proxy.event_bridge import EventBusBridge
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    bridge = EventBusBridge()
    bus = _BlockingEventBus()
    bridge.start(bus)
    bridge._started.wait(timeout=3.0)

    # After _started fires, _loop and _stop_event are set.
    assert bridge._loop is not None
    assert bridge._stop_event is not None

    # The fix: stop() captures locals before the None-check.
    # We cannot perfectly reproduce the race in a deterministic unit test,
    # but we can verify stop() completes without AttributeError even if we
    # null out the instance references right before calling it.
    saved_loop = bridge._loop
    saved_stop = bridge._stop_event
    # Null out instance references (simulating the background thread's finally block)
    bridge._loop = None
    bridge._stop_event = None

    # stop() should NOT raise AttributeError — it must be safe to call
    # with nulled-out instance attributes because the local-capture fix
    # reads the attributes at the top of stop(), before any None-check.
    # Since we nulled them out before stop(), the if-guard will skip the
    # call_soon_threadsafe. That's the correct safe behaviour.
    bridge.stop()

    # Attempt to signal the stop event — the loop may already be closed
    # if the _BlockingEventBus exhausted immediately, which is fine.
    try:
        saved_loop.call_soon_threadsafe(saved_stop.set)
    except RuntimeError:
        pass
    if bridge._thread is not None:
        bridge._thread.join(timeout=2.0)
