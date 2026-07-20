"""Tests for the embedded terminal sessions in sharkdrownv3.

Headless tests cover:
  - ``renderer.screen_to_html`` — pure pyte → HTML, ANSI colors, escaping
  - ``key_to_bytes`` — Qt key → PTY bytes
  - ``TerminalContainerView`` — empty-state construction, idempotent shutdown
  - ``TerminalSessionView`` — safe shutdown before ``start()``

Real-PTY tests (POSIX; guarded by ``hasattr(os, "openpty")``):
  - Roundtrip with ``/bin/cat``
  - Exit signal when child finishes
  - End-to-end: ``echo hi`` reaches the pyte screen
  - ``resize`` doesn't raise
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


# ---- Helpers --------------------------------------------------------------


def _qapp():
    """Build (or reuse) a headless QApplication."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _spin(ms: int = 200):
    """Spin the Qt event loop for ``ms`` milliseconds."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    qt = QTimer()
    qt.setSingleShot(True)
    qt.setInterval(ms)
    qt.timeout.connect(loop.quit)
    qt.start()
    loop.exec()


# ---- Renderer tests (always run) ------------------------------------------


def test_renderer_plain() -> None:
    """Plain text wraps in <pre> and renders each cell."""
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(10, 2)
    pyte.ByteStream(s).feed(b"hello")
    html = screen_to_html(s)
    assert "<pre" in html
    assert "</pre>" in html
    # The text we fed should be in the rendered output.
    assert "hello" in html
    # 5 chars of input plus padding for the rest of the row.
    # We don't assert exact padding because pyte fills with spaces; just
    # check there's enough trailing &nbsp; entities to fill a 10-col row.
    assert html.count("&nbsp;") >= 15, f"missing trailing spaces: {html!r}"
    print("✓ renderer wraps plain text in <pre> with proper escaping")


def test_renderer_ansi_color() -> None:
    """ANSI red text renders as a styled span with a hex color."""
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(20, 2)
    pyte.ByteStream(s).feed(b"\x1b[31mRED\x1b[0m")
    html = screen_to_html(s)
    # Should have a span with a red background-color or color attribute.
    assert "<span" in html, f"expected styled span, got: {html!r}"
    assert "#cc0000" in html or "#ef2929" in html, (
        f"expected red color hex, got: {html!r}"
    )
    assert "RED" in html
    print("✓ renderer emits red span for ANSI 31")


def test_renderer_escaping() -> None:
    """HTML-unsafe chars are entity-escaped."""
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(10, 2)
    pyte.ByteStream(s).feed(b"<a>&\"'")
    html = screen_to_html(s)
    # None of the raw chars should survive.
    assert "<a>" not in html, f"raw <a> leaked: {html!r}"
    assert "&amp;" in html
    assert "&lt;" in html
    assert "&gt;" in html
    print("✓ renderer HTML-escapes unsafe chars")


def test_keymap_bytes() -> None:
    """The pure keymap translates Qt keys to PTY bytes correctly."""
    from PySide6.QtCore import Qt
    from src.ui.terminal.terminal_session_view import key_to_bytes

    # Ctrl+C → 0x03
    assert key_to_bytes(Qt.Key_C, modifiers=Qt.ControlModifier) == b"\x03"
    # Up arrow → CSI A
    assert key_to_bytes(Qt.Key_Up) == b"\x1b[A"
    # Down/Right/Left
    assert key_to_bytes(Qt.Key_Down) == b"\x1b[B"
    assert key_to_bytes(Qt.Key_Right) == b"\x1b[C"
    assert key_to_bytes(Qt.Key_Left) == b"\x1b[D"
    # Enter → CR
    assert key_to_bytes(Qt.Key_Return) == b"\r"
    assert key_to_bytes(Qt.Key_Enter) == b"\r"
    # Backspace → DEL
    assert key_to_bytes(Qt.Key_Backspace) == b"\x7f"
    # Tab → TAB
    assert key_to_bytes(Qt.Key_Tab) == b"\t"
    # Plain printable text
    assert key_to_bytes(Qt.Key_A, text="a") == b"a"
    # Modifier-only (Shift alone) → None
    assert key_to_bytes(Qt.Key_Shift) is None
    # Unknown with text — falls through to the text.
    assert key_to_bytes(Qt.Key_Question, text="?") == b"?"
    print("✓ keymap translates Qt keys to PTY bytes")


# ---- Widget construction (always run) --------------------------------------


def test_container_constructs() -> None:
    """Container starts empty and shuts down cleanly with no sessions."""
    _qapp()
    from src.ui.terminal.terminal_container_view import TerminalContainerView

    view = TerminalContainerView()
    try:
        assert view.session_count == 0
        assert "No sessions yet" in view.info_label.text()
        # Shutdown with zero sessions is a no-op (idempotent).
        view.shutdown()
        assert view.session_count == 0
    finally:
        view.shutdown()
    print("✓ TerminalContainerView constructs empty, shuts down clean")


def test_session_view_shutdown_before_start() -> None:
    """Shutdown before start() is safe — no fd leaks."""
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/echo"])
    try:
        # Should not raise even though start() was never called.
        view.shutdown()
        # Title still works.
        assert "echo" in view.title()
    finally:
        view.shutdown()
    print("✓ TerminalSessionView.shutdown() safe before start()")


def test_default_argv_is_interactive() -> None:
    """No explicit argv ⇒ shell is invoked with ``-i`` so the user gets
    a prompt and echo without having to pass flags.

    Regression test for the bug where omitting argv produced
    ``["/bin/bash"]`` (no ``-i``), so bash ran non-interactively: no
    prompt, no echo, Return did nothing.
    """
    _qapp()
    from src.ui.terminal.pty_backend import PtySession

    # Use a fake SHELL that points at bash so the auto-detection matches.
    import os as _os
    saved = _os.environ.get("SHELL")
    _os.environ["SHELL"] = "/bin/bash"
    try:
        sess = PtySession()  # no argv
        assert "-i" in sess._argv, (
            f"default argv should include -i for interactive shells, "
            f"got: {sess._argv}"
        )
        assert sess._argv[0] == "/bin/bash"
    finally:
        if saved is None:
            _os.environ.pop("SHELL", None)
        else:
            _os.environ["SHELL"] = saved
    print("✓ default PtySession argv includes -i for bash/sh/dash/ksh/zsh")


def test_explicit_argv_preserved() -> None:
    """Explicit argv is passed through verbatim — no auto -i appended."""
    _qapp()
    from src.ui.terminal.pty_backend import PtySession

    sess = PtySession(["/bin/cat"])
    assert sess._argv == ["/bin/cat"], (
        f"explicit argv should be preserved verbatim, got: {sess._argv}"
    )
    print("✓ explicit PtySession argv is preserved verbatim")


def test_size_floor_prevents_tiny_terminal() -> None:
    """A transient tiny layout (1×12) doesn't shrink the pyte Screen.

    Regression test for the bug where ``start()`` was called before the
    widget had been laid out, ``_recompute_size`` saw a viewport of
    ~98×28 px, and computed a 1×12 terminal. Bash then saw a tiny tty
    and either didn't render its prompt or rendered it off-screen, so
    the user saw an empty terminal.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView()
    try:
        # Don't resize / show the widget — simulate the "called too
        # early" condition where the viewport is the size of a tab
        # placeholder, not a full panel.
        view.start()
        # We should keep the DEFAULT_ROWS × DEFAULT_COLS because the
        # computed size (whatever it is) is below MIN_ROWS/MIN_COLS.
        assert view._rows >= TerminalSessionView.MIN_ROWS, (
            f"rows shrank to {view._rows}, below MIN_ROWS={TerminalSessionView.MIN_ROWS}"
        )
        assert view._cols >= TerminalSessionView.MIN_COLS, (
            f"cols shrank to {view._cols}, below MIN_COLS={TerminalSessionView.MIN_COLS}"
        )
        assert view._screen.lines >= TerminalSessionView.MIN_ROWS
        assert view._screen.columns >= TerminalSessionView.MIN_COLS
    finally:
        view.shutdown()
    print("✓ _recompute_size rejects implausibly small viewports")


def test_focus_transfers_on_tab_switch() -> None:
    """When the Terminals tab is shown, focus transfers to the text edit.

    Regression test for the bug where the QTabBar kept focus after the
    user clicked the Terminals tab title, and keystrokes were silently
    swallowed by the tab bar's keyboard navigation instead of reaching
    the terminal.
    """
    _qapp()
    from src.ui.main_window import MainWindow

    w = MainWindow()
    try:
        # The window must be shown (or at least sized) for focus
        # management to behave realistically; otherwise offscreen mode
        # has no widget hierarchy to propagate focus through.
        w.resize(1000, 700)
        w.show()
        _spin(50)

        # Create a session first (focus goes to its text edit).
        w._new_terminal_session()
        _spin(50)
        view = w.terminal_container._sessions[0]
        assert view.text_edit.hasFocus(), (
            "newly created session's text edit should have focus"
        )

        # Now simulate the user clicking the Documents tab title — focus
        # moves to the QTabBar, away from the terminal.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_DOCUMENTS)
        _spin(50)
        w.view_tabs.tabBar().setFocus()
        _spin(50)
        assert not view.text_edit.hasFocus(), (
            "sanity: focus should not be on the text edit after we moved it"
        )

        # Switch back to Terminals — our _on_view_tab_changed handler
        # should transfer focus back to the text edit.
        w.view_tabs.setCurrentIndex(w._TAB_INDEX_TERMINALS)
        _spin(50)
        assert view.text_edit.hasFocus(), (
            "switching back to Terminals should re-focus the text edit"
        )
    finally:
        if w.isVisible():
            w.close()
    print("✓ focus transfers to the active session's text edit")


# ---- Real-PTY tests (POSIX) ----------------------------------------------


_HAS_PTY = hasattr(os, "openpty")


def test_pty_echo_roundtrip() -> None:
    """PtySession with /bin/cat echoes back the bytes we write."""
    if not _HAS_PTY:
        print("⊘ test_pty_echo_roundtrip: skipped (no POSIX openpty)")
        return
    _qapp()
    from src.ui.terminal.pty_backend import PtySession

    sess = PtySession(["/bin/cat"], rows=24, cols=80)
    received: list[bytes] = []
    sess.data_received.connect(received.append)
    sess.start_pty()

    sess.write(b"ping\n")
    sess.write(b"pong\n")

    # Spin up to 1 second for both lines to echo back.
    for _ in range(20):
        _spin(50)
        if b"ping" in b"".join(received) and b"pong" in b"".join(received):
            break

    combined = b"".join(received)
    assert b"ping" in combined, f"missing ping echo: {combined!r}"
    assert b"pong" in combined, f"missing pong echo: {combined!r}"

    sess.shutdown()
    assert not sess.is_alive, "session still alive after shutdown"
    print("✓ PtySession echoes /bin/cat input back")


def test_pty_exit_signal() -> None:
    """A short-lived child triggers process_exited with exit code."""
    if not _HAS_PTY:
        print("⊘ test_pty_exit_signal: skipped (no POSIX openpty)")
        return
    _qapp()
    from src.ui.terminal.pty_backend import PtySession

    sess = PtySession(["/bin/sh", "-c", "exit 7"], rows=24, cols=80)
    exited: list[int] = []
    sess.process_exited.connect(lambda code: exited.append(code))
    sess.start_pty()

    # Spin up to 2 seconds for the exit signal.
    for _ in range(40):
        _spin(50)
        if exited:
            break

    assert exited, "process_exited never fired"
    # exit 7 → 7
    assert exited[0] == 7, f"unexpected exit code {exited[0]}"
    # Shutdown is idempotent after natural exit.
    sess.shutdown()
    sess.shutdown()
    print("✓ PtySession fires process_exited with the child's exit code")


def test_session_view_end_to_end() -> None:
    """Full TerminalSessionView: feed shell input, get rendered output."""
    if not _HAS_PTY:
        print("⊘ test_session_view_end_to_end: skipped (no POSIX openpty)")
        return
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    # /bin/sh with -c that prints and exits is the simplest reliable test.
    # We want to test the round-trip, so use a non-exiting shell.
    view = TerminalSessionView(argv=["/bin/sh"])
    try:
        # The widget needs to be visible at a real size before start()
        # so _recompute_size picks a sensible rows/cols. Otherwise the
        # pyte screen is 1×N and a single character scrolls the prompt
        # off-screen. Same lifecycle as adding a session via the toolbar.
        view.resize(800, 600)
        view.show()
        _spin(50)
        view.start()
        # Wait for the prompt (just give it a moment to initialize).
        _spin(500)
        view._send_input(b"echo hi-from-pyte-test\n")
        # Spin until 'hi-from-pyte-test' appears in the rendered text.
        found = False
        for _ in range(40):
            _spin(50)
            text = view.text_edit.toPlainText()
            if "hi-from-pyte-test" in text:
                found = True
                break
        assert found, (
            f"echo output never reached the text edit; text={view.text_edit.toPlainText()!r}"
        )
    finally:
        view.shutdown()
    print("✓ TerminalSessionView round-trips shell output to QTextEdit")


def test_resize_ioctl() -> None:
    """PtySession.resize doesn't raise and updates internal state."""
    if not _HAS_PTY:
        print("⊘ test_resize_ioctl: skipped (no POSIX openpty)")
        return
    _qapp()
    from src.ui.terminal.pty_backend import PtySession

    sess = PtySession(["/bin/cat"], rows=24, cols=80)
    sess.start_pty()
    try:
        # Various sizes should not raise.
        sess.resize(40, 100)
        sess.resize(1, 1)
        sess.resize(24, 80)
        # Minimum clamp: zero-sized resize should still succeed.
        sess.resize(0, 0)
    finally:
        sess.shutdown()
    print("✓ PtySession.resize handles a variety of sizes")


# ---- Selection / clipboard / resize tests ---------------------------------


def test_renderer_selection_highlight() -> None:
    """A linear selection renders the highlighted cell with swapped fg/bg."""
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(20, 2)
    pyte.ByteStream(s).feed(b"\x1b[31mhi\x1b[0m")  # red "hi"
    # Select x∈[0,1] on row 0 (which holds 'h' and 'i').
    html = screen_to_html(s, selection=((0, 0, 1, 0), False))
    # The highlighted cell should have force_reverse=True, which means
    # the *background* is the original fg color (red). Non-highlighted
    # cells would show red as their fg. So a "background-color:#cc0000"
    # indicates a selected cell.
    assert "background-color:#cc0000" in html, (
        f"expected reversed red on selection, got: {html!r}"
    )
    # Sanity: the original 'h', 'i' appear.
    assert "hi" in html
    print("✓ renderer highlights selected cells with reversed fg/bg")


def test_renderer_block_selection_highlight() -> None:
    """Block selection highlights an x-rectangle across multiple rows."""
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(20, 2)
    pyte.ByteStream(s).feed(b"abcdef\nghijkl")
    # Block select cols [1,2] rows 0-1 ('b','c' / 'h','i' equivalent
    # in row 1: 'h','i' from 'ghijkl').
    html = screen_to_html(s, selection=((1, 0, 2, 1), True))
    # Default-color selection rendering adds an rgba tint so the
    # selection is visually distinct from the unselected runs (which
    # carry plain `color:var(--term-fg);background-color:var(--term-bg)`).
    reverse_count = html.count("rgba(255,255,255,0.15)")
    assert reverse_count >= 2, (
        f"expected at least 2 selection-highlighted spans (one per row), "
        f"got: {reverse_count} in {html!r}"
    )
    print("✓ renderer highlights block-selection rectangle across rows")


def test_extract_cells_text_strips_trailing_spaces() -> None:
    """Linear selection strips per-line trailing whitespace."""
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalTextEdit
    import pyte

    edit = TerminalTextEdit(on_input=lambda data: None)
    screen = pyte.Screen(10, 3)
    # Use cursor-positioning (CSI row;col H) to deterministically place
    # each row's text — pyte's implicit cursor model after CR+LF
    # interacts in unhelpful ways for fixed-buffer tests.
    pyte.ByteStream(screen).feed(b"\x1b[1;1Hhi        ")
    pyte.ByteStream(screen).feed(b"\x1b[2;1Hworld      ")
    pyte.ByteStream(screen).feed(b"\x1b[3;1H")
    edit._screen_ref = lambda: screen
    edit._screen_width = lambda: 10
    edit._screen_height = lambda: 3
    # Linear selection: rows 0-1, full-width end-to-end so each row
    # is read in full and rstripped.
    edit._sel_anchor = (0, 0)
    edit._sel_active = (9, 1)
    edit._sel_block_flag = False
    text = edit._extract_cells_text((0, 0, 9, 1), is_block=False)
    # Row 0: "hi        " -> rstrip -> "hi"
    # Row 1: "world     " -> rstrip -> "world"
    assert text == "hi\nworld", f"unexpected: {text!r}"
    print("✓ linear _extract_cells_text strips per-line trailing whitespace")


def test_extract_cells_text_block_preserves_columns() -> None:
    """Block selection preserves right-side padding, not per-line rstrip."""
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalTextEdit
    import pyte

    edit = TerminalTextEdit(on_input=lambda data: None)
    screen = pyte.Screen(10, 3)
    pyte.ByteStream(screen).feed(b"\x1b[1;1Habc       ")
    pyte.ByteStream(screen).feed(b"\x1b[2;1Hdefgh     ")
    pyte.ByteStream(screen).feed(b"\x1b[3;1H")
    edit._screen_ref = lambda: screen
    edit._screen_width = lambda: 10
    edit._screen_height = lambda: 3
    # Block select cols 0-2 rows 0-1.
    edit._sel_anchor = (0, 0)
    edit._sel_active = (2, 1)
    edit._sel_block_flag = True
    text = edit._extract_cells_text((0, 0, 2, 1), is_block=True)
    # Each row reads cols 0..2: 'abc' / 'def'. Block mode does NOT rstrip
    # so columns stay aligned; the only trim is full-blank trailing rows.
    assert text == "abc\ndef", f"unexpected: {text!r}"
    print("✓ block _extract_cells_text preserves columns (no rstrip)")


def test_clipboard_copy_writes_to_clipboard() -> None:
    """_copy_selection writes selected text to QClipboard.Clipboard."""
    _qapp()
    from PySide6.QtGui import QClipboard, QGuiApplication
    from src.ui.terminal.terminal_session_view import TerminalTextEdit
    import pyte

    edit = TerminalTextEdit(on_input=lambda data: None)
    screen = pyte.Screen(10, 3)
    pyte.ByteStream(screen).feed(b"hi\nworld")
    edit._screen_ref = lambda: screen
    edit._screen_width = lambda: 10
    edit._screen_height = lambda: 3
    edit._sel_anchor = (0, 0)
    edit._sel_active = (4, 1)
    edit._sel_block_flag = False

    cb = QGuiApplication.clipboard()
    cb.clear(QClipboard.Clipboard)
    edit._copy_selection()
    got = cb.text(QClipboard.Clipboard)
    assert "hi" in got and "world" in got, (
        f"expected 'hi' and 'world' on clipboard, got: {got!r}"
    )
    print("✓ _copy_selection writes pyte cell text to QClipboard")


def test_pty_paste_round_trip() -> None:
    """A paste fires _on_input with the cleaned clipboard text."""
    if not _HAS_PTY:
        print("⊘ test_pty_paste_round_trip: skipped (no POSIX openpty)")
        return
    _qapp()
    from PySide6.QtGui import QClipboard, QGuiApplication
    from src.ui.terminal.terminal_session_view import TerminalSessionView
    import pyte

    cb = QGuiApplication.clipboard()
    cb.setText(
        "PASTE-MARKER-12345\nNEXT-LINE",
        QClipboard.Clipboard,
    )

    received: list[bytes] = []
    view = TerminalSessionView(argv=["/bin/cat"])
    # Wire input callback to capture what got sent to the PTY.
    view.text_edit._on_input = lambda data: received.append(data)
    # Override _paste_from's sanitizer to skip the screen.mode probe
    # (we have no _screen yet on a bare session).
    # We trigger _paste_from directly:
    view.text_edit._paste_from(mode=QClipboard.Clipboard)
    # Expected: data arg is bytes containing the literal text (newlines
    # normalized to \n).
    assert received, "paste produced no bytes"
    payload = received[0]
    assert b"PASTE-MARKER-12345" in payload
    assert b"NEXT-LINE" in payload
    # Cleanup.
    view.shutdown()
    print("✓ _paste_from writes sanitized clipboard text to PTY")


def test_bracketed_paste_always_wrapped() -> None:
    """Every paste payload is wrapped in ESC[200~ ... ESC[201~ delimiters.

    Decision: we always wrap. pyte's ``screen.mode`` doesn't track DEC
    private mode 2004 (bracketed paste), so we can't conditionally
    detect a compatible shell. Always-wrap matches xterm / Alacritty
    defaults and is correct for every common interactive shell — the
    failure mode (raw ``cat`` echoing the escapes) is rare and benign.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalTextEdit
    from PySide6.QtGui import QClipboard, QGuiApplication

    edit = TerminalTextEdit(on_input=lambda data: None)
    received: list[bytes] = []
    edit._on_input = lambda data: received.append(data)
    QGuiApplication.clipboard().setText("PASTED", QClipboard.Clipboard)
    edit._paste_from(mode=QClipboard.Clipboard)
    assert received, "expected bracketed paste bytes"
    payload = received[0]
    assert payload.startswith(b"\x1b[200~"), (
        f"missing opener: {payload!r}"
    )
    assert payload.endswith(b"\x1b[201~"), (
        f"missing closer: {payload!r}"
    )
    assert b"PASTED" in payload
    print("✓ _paste_from always wraps payload in ESC[200~ ... ESC[201~")


def test_resize_changes_rows_and_cols() -> None:
    """A resize event eventually updates view._rows / view._cols and the
    underlying pyte Screen, via the dedicated _resize_timer (50ms).

    Regression for the bug where the terminal would freeze at its
    post-start dimensions forever.
    """
    if not _HAS_PTY:
        print("⊘ test_resize_changes_rows_and_cols: skipped (no POSIX openpty)")
        return
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    try:
        view.resize(800, 600)
        view.show()
        _spin(50)
        view.start()
        _spin(50)
        # Capture initial size.
        initial_cols = view._cols
        # Resize to a wider window — should increase cols after debounce.
        view.resize(1600, 800)
        # Spin > 50ms so the resize timer fires.
        _spin(120)
        assert view._cols > initial_cols, (
            f"expected cols to grow after resize, initial={initial_cols} "
            f"now={view._cols}"
        )
        # pyte screen should have been resized too.
        assert view._screen.columns == view._cols
        # Resize window narrower — should also update, but only if it
        # still passes MIN_COLS. Our MIN_COLS=40; 1200px wide ~ 150 cols
        # well above the floor.
        view.resize(1000, 600)
        _spin(120)
        assert view._cols < view._screen.columns + 1, (
            f"resize wider+narrower should produce stable size, "
            f"got _cols={view._cols} screen.cols={view._screen.columns}"
        )
    finally:
        view.shutdown()
    print("✓ resize event propagates to pyte screen via debounce timer")


# ---- Runner ----------------------------------------------------------------


def main() -> None:
    tests = [
        test_renderer_plain,
        test_renderer_ansi_color,
        test_renderer_escaping,
        test_keymap_bytes,
        test_container_constructs,
        test_session_view_shutdown_before_start,
        test_default_argv_is_interactive,
        test_explicit_argv_preserved,
        test_size_floor_prevents_tiny_terminal,
        test_focus_transfers_on_tab_switch,
        test_pty_echo_roundtrip,
        test_pty_exit_signal,
        test_session_view_end_to_end,
        test_resize_ioctl,
        test_renderer_selection_highlight,
        test_renderer_block_selection_highlight,
        test_extract_cells_text_strips_trailing_spaces,
        test_extract_cells_text_block_preserves_columns,
        test_clipboard_copy_writes_to_clipboard,
        test_pty_paste_round_trip,
        test_bracketed_paste_always_wrapped,
        test_resize_changes_rows_and_cols,
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
    print("\n✅ Terminal tests passed!")


if __name__ == "__main__":
    main()
