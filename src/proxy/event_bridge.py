"""Qt signal bridge for the in-process EventBus.

The proxy's asyncio EventBus publishes dict events from the uvicorn
thread. This bridge subscribes to the bus and re-emits the events as
Qt signals on the main thread — no HTTP polling needed.

Usage from the MainWindow / proxy lifecycle:

    bridge = EventBusBridge()
    bridge.start(event_bus)  # call after proxy startup
    bridge.request_created.connect(activity_tile.on_request_created)
    bridge.response_complete.connect(activity_tile.on_response_complete)
    bridge.stop()            # call on proxy shutdown
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger("sharkdrown.event_bridge")


class EventBusBridge(QObject):
    """Subscribe to the asyncio EventBus and emit Qt signals."""

    request_created = Signal(dict)
    response_complete = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        # Set by the background thread once _loop and _stop_event are ready.
        # stop() waits on this before calling call_soon_threadsafe so it
        # never races against an uninitialised stop event (#24).
        self._started = threading.Event()

    def start(self, event_bus: Any) -> None:
        """Begin consuming events from *event_bus* in a background thread."""
        if self._thread is not None:
            return
        self._started.clear()
        self._thread = threading.Thread(
            target=self._run, args=(event_bus,), daemon=True, name="event-bridge"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the consumer to stop and wait for the thread to exit."""
        if self._thread is not None:
            # Wait until the background thread has set _loop and _stop_event.
            # Timeout guards against the thread dying before it reaches that
            # point — in that case we still clean up via join below.
            self._started.wait(timeout=5.0)
            if self._loop is not None and self._stop_event is not None:
                try:
                    self._loop.call_soon_threadsafe(self._stop_event.set)
                except RuntimeError:
                    # Loop already closed — thread is exiting naturally (#25).
                    pass
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self, event_bus: Any) -> None:
        """Thread target — runs its own asyncio event loop."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        # Signal that _loop and _stop_event are ready for stop() to use.
        self._started.set()
        try:
            loop.run_until_complete(self._consume(event_bus, stop_event))
        except Exception:  # noqa: BLE001
            logger.debug("event bridge loop exited", exc_info=True)
        finally:
            loop.close()
            self._loop = None

    async def _consume(self, event_bus: Any, stop_event: asyncio.Event) -> None:
        """Async consumer — yields events until stop is requested."""
        subscriber = event_bus.subscribe()
        try:
            async for event in subscriber:
                if stop_event.is_set():
                    break
                self._dispatch(event)
        except asyncio.CancelledError:
            pass

    def _dispatch(self, event: dict[str, Any]) -> None:
        """Emit the appropriate Qt signal based on event type."""
        event_type = event.get("type")
        if event_type == "request_created":
            self.request_created.emit(event)
        elif event_type == "response_complete":
            self.response_complete.emit(event)
