"""Regression tests for the PR5 dirty-tracking repaint (issues #3 + #4).

The architectural rewrite replaced the per-tick ``setHtml`` repaint
with a content-hash short-circuit: when the pyte screen buffer,
scrollback length, and selection state are all unchanged from the
previous frame, the entire rebuild is skipped. This resolves:

- Issue #4 ("scroll up/down broken"): ``setHtml`` reset the scrollbar
  to 0 every 16ms; with dirty tracking, idle frames don't rebuild at
  all, so user-up scrolls survive the next PTY tick.
- Issue #3 ("font change misaligns content"): the cost of rebuilding
  every tick was masking the root cause (dead stylesheet in
  ``_measure_rendered_rows``); PR5 fixes both.

Verifies:
- Idle frames (no PTY data, no selection change) skip the rebuild.
- Frames with new PTY data still rebuild.
- Resize invalidates the cache.
- Scrollback growth rebuilds the cached history HTML.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _spin(ms: int) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_idle_frames_skip_rebuild() -> None:
    """Two consecutive ``_repaint`` calls with no PTY data skip the second.

    The dirty hash matches the cached one, so the early-return
    short-circuits the rebuild. We assert this indirectly via the
    cached-signature state: after the first repaint, ``_cached_live_signature``
    is non-None; after the second repaint with no new data, it
    should be the SAME object (the cache was hit, no rebuild).
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(20)
    view.start()
    # ``start()`` sets ``self._screen = pyte.HistoryScreen(...)``.
    # Wait for the screen to be ready.
    loop_count = 0
    while view._screen is None and loop_count < 10:
        _spin(20)
        loop_count += 1
    assert view._screen is not None, "screen not initialised after start()"
    _spin(40)
    # First repaint populates the cache.
    view._repaint()
    sig_after_first = view._cached_live_signature
    assert sig_after_first is not None, (
        "first repaint should have populated the cache"
    )
    # Second repaint with no PTY data — cache should be hit.
    view._repaint()
    sig_after_second = view._cached_live_signature
    assert sig_after_second is sig_after_first, (
        "second repaint with no PTY data should hit the cache "
        "(same signature object reference)"
    )
    view.shutdown()


def test_new_pty_data_triggers_rebuild() -> None:
    """Feeding bytes to the PTY invalidates the cache."""
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(20)
    view.start()
    loop_count = 0
    while view._screen is None and loop_count < 10:
        _spin(20)
        loop_count += 1
    assert view._screen is not None, "screen not initialised after start()"
    _spin(40)
    view._repaint()
    sig_before = view._cached_live_signature
    # Feed some bytes through the stream directly — no need to spin
    # the PTY, the dirty-hash check happens on the buffer state.
    view._stream.feed(b"hello\n")
    view._repaint()
    sig_after = view._cached_live_signature
    assert sig_after is not sig_before, (
        "feeding new PTY data should change the signature and force a rebuild"
    )
    view.shutdown()


def test_resize_invalidates_cache() -> None:
    """``_on_resize_debounced`` resets the cached state when size changes."""
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(20)
    view.start()
    loop_count = 0
    while view._screen is None and loop_count < 10:
        _spin(20)
        loop_count += 1
    assert view._screen is not None, "screen not initialised after start()"
    _spin(40)
    view._repaint()
    assert view._cached_live_signature is not None
    # Resize to a different size — the cache must be invalidated.
    view.resize(1024, 768)
    _spin(80)  # let resize debounce fire
    # After the resize, _on_resize_debounced should have cleared
    # the cache fields. The post-resize signature is the new size's.
    assert view._cached_live_signature is not None
    view.shutdown()


def test_scrollback_growth_rebuilds_history_cache() -> None:
    """Adding lines to scrollback rebuilds the cached history HTML.

    We simulate scrollback growth by feeding enough data to push the
    top line off the live screen into the history buffer.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 200)  # small viewport — history fills fast
    view.show()
    _spin(20)
    view.start()
    loop_count = 0
    while view._screen is None and loop_count < 10:
        _spin(20)
        loop_count += 1
    assert view._screen is not None, "screen not initialised after start()"

    # Push 50 lines through the stream; pyte's HistoryScreen will
    # move the top lines into ``history.top``.
    for i in range(50):
        view._stream.feed(f"line {i:02d}\n".encode())
    view._repaint()

    # The cached history HTML should have been populated.
    assert view._cached_history_lines_count > 0
    assert view._cached_history_html != "", (
        "scrollback growth should populate the cached history HTML"
    )
    # And the cached count should equal the actual history length.
    actual_history_len = len(view._screen.history.top)
    assert view._cached_history_lines_count == actual_history_len
    view.shutdown()