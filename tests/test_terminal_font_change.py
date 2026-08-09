"""Regression tests for font-size change in claude-code sessions.

User-reported symptom: when increasing/decreasing the claude-code
session font size, the TUI is lost, leaving a half-displayed or
blank screen. Reducing to the original font size does NOT resolve
the issue — the session appears broken until restart.

Likely root causes (multiple, since the symptom is severe):
1. ``_recompute_size`` clamping at MIN_COLS=40 even when the
   viewport is too narrow at the new font size → grid is larger
   than viewport → content clipped → "half-displayed".
2. ``_recompute_size`` returning 0 rows for a too-tall font →
   ``_session.resize(0, x)`` sends TIOCSWINSZ with 0 rows → shell
   can deadlock or render garbled.
3. Cell grid freeze: PR5's earlier fix unconditionally returned
   when cols/rows were below MIN_*, freezing the grid at the old
   (small) size; new font → document doesn't fit.

Tests:
- Font change keeps ``_rows`` and ``_cols`` valid (>0) and within
  the viewport (cols*cw <= w, rows*ch <= h).
- The session is still able to receive input after a font change
  (PTY is not killed by TIOCSWINSZ with garbage values).
- A second font change (back to original) restores the original
  rows/cols (not a stuck value).
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


def test_font_change_keeps_rows_cols_within_viewport() -> None:
    """After ``adjust_font_size``, the new (rows, cols) MUST fit the
    viewport at the new font size.

    Pre-PR5: at 28pt font on 800×600, the freeze would keep
    ``cols=80`` while ``cw=22`` → ``80*22=1760px`` overflows the
    800px viewport (visible as "half-displayed TUI").

    Post-PR5 (correct fix): ``cols`` shrinks to whatever fits the
    viewport, never exceeds it.
    """
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
    assert view._screen is not None
    _spin(40)
    view._recompute_size()
    initial_rows = view._rows
    initial_cols = view._cols

    # Bump font up by 8 (14pt → 22pt-ish).
    for _ in range(8):
        view.adjust_font_size(+1)
    _spin(40)
    view._recompute_size()

    # After the font change, the grid must fit the viewport.
    fm = view.text_edit.fontMetrics()
    cw = max(1, fm.horizontalAdvance("M"))
    ch = max(1, fm.lineSpacing())
    w = view.text_edit.viewport().width()
    h = view.text_edit.viewport().height()
    assert view._rows > 0, f"rows={view._rows} (zero is invalid)"
    assert view._cols > 0, f"cols={view._cols} (zero is invalid)"
    assert view._rows * ch <= h + 1, (
        f"rows={view._rows}, ch={ch}, total={view._rows * ch}, "
        f"viewport_h={h}: rows don't fit the viewport — TUI "
        f"will be clipped vertically"
    )
    assert view._cols * cw <= w + 1, (
        f"cols={view._cols}, cw={cw}, total={view._cols * cw}, "
        f"viewport_w={w}: cols don't fit the viewport — TUI "
        f"will be clipped horizontally (the 'half-displayed' symptom)"
    )
    # Verify the grid actually changed (otherwise the test isn't testing anything).
    assert (view._rows, view._cols) != (initial_rows, initial_cols), (
        f"font change should have re-derived rows/cols but they're "
        f"unchanged: {(initial_rows, initial_cols)}"
    )
    view.shutdown()


def test_font_change_back_to_original_restores_grid() -> None:
    """Reducing the font back to its original size restores the original
    (rows, cols) — the grid doesn't get stuck after multiple changes.

    User symptom: "reducing to the original font size does not resolve
    the issue". If after a font-up + font-down cycle the grid is
    stuck at a non-original value, the user sees a permanent
    misalignment.
    """
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
    assert view._screen is not None
    _spin(40)
    view._recompute_size()
    initial_grid = (view._rows, view._cols)

    # Bump up, then back down.
    for _ in range(5):
        view.adjust_font_size(+1)
    _spin(40)
    view._recompute_size()
    bigger_grid = (view._rows, view._cols)

    for _ in range(5):
        view.adjust_font_size(-1)
    _spin(40)
    view._recompute_size()
    final_grid = (view._rows, view._cols)

    # After the round-trip, the grid should be back to (or very close to)
    # the original — same font, same viewport, same math.
    assert final_grid == initial_grid, (
        f"font round-trip should restore the original grid, but: "
        f"initial={initial_grid}, after-up={bigger_grid}, "
        f"after-round-trip={final_grid}. Stuck grid is the "
        f"'reducing to original does not resolve' symptom."
    )
    view.shutdown()


def test_font_change_does_not_kill_session() -> None:
    """The session must still be alive (PTY not closed) after a font
    change. ``_session.resize()`` with bad values could conceivably
    close the PTY fd if the ioctl raises in a specific way; this
    test catches that.
    """
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
    assert view._screen is not None
    _spin(40)
    view._recompute_size()

    assert view._session is not None and view._started and not view._exited

    # Bump font 5x.
    for _ in range(5):
        view.adjust_font_size(+1)
    _spin(40)

    assert view._session is not None, "session was None after font change"
    assert not view._exited, (
        "session exited after font change — TIOCSWINSZ with bad "
        "values may have killed the PTY fd"
    )
    assert view._started, "session was un-started after font change"
    view.shutdown()


def test_font_change_zero_rows_or_cols_not_sent() -> None:
    """TIOCSWINSZ must never be called with rows=0 or cols=0 — those
    values can detach the shell from the TTY and lock the session
    in a non-recoverable state.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    sent_calls: list[tuple[int, int]] = []

    class _FakeSession:
        def resize(self, rows: int, cols: int) -> None:
            sent_calls.append((rows, cols))

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(20)
    view.start()
    loop_count = 0
    while view._screen is None and loop_count < 10:
        _spin(20)
        loop_count += 1
    assert view._screen is not None
    _spin(40)
    # Inject a fake session so we capture every resize call without
    # needing a real PTY / container.
    view._session = _FakeSession()  # type: ignore[assignment]
    view._started = True
    view._exited = False

    # Try a series of font bumps and shrinks.
    for _ in range(10):
        view.adjust_font_size(+1)
    for _ in range(10):
        view.adjust_font_size(-1)
    _spin(40)
    view._recompute_size()

    # Every resize call must have rows >= 1 and cols >= 1.
    for rows, cols in sent_calls:
        assert rows >= 1, (
            f"TIOCSWINSZ sent with rows={rows} (must be >= 1; "
            f"0 rows detaches the shell from the TTY)"
        )
        assert cols >= 1, (
            f"TIOCSWINSZ sent with cols={cols} (must be >= 1; "
            f"0 cols detaches the shell from the TTY)"
        )
    assert sent_calls, "no resize calls were captured"
    view.shutdown()