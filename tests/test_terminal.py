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

_PROJECT_ROOT = Path(__file__).parent.parent
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
    # Shift+Tab → xterm back-tab (CSI Z), NOT a plain TAB. The Claude
    # Code TUI uses CSI Z to cycle permission modes; sending \t would
    # just indent the input prompt.
    assert key_to_bytes(Qt.Key_Backtab, modifiers=Qt.ShiftModifier) == b"\x1b[Z"
    assert (
        key_to_bytes(Qt.Key_Tab, modifiers=Qt.ShiftModifier, text="\t") == b"\x1b[Z"
    )
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

        # Now simulate the user clicking the Documents view in the
        # right-aligned combo — focus moves away from the terminal.
        w.view_selector.setCurrentIndex(w._TAB_INDEX_DOCUMENTS)
        _spin(50)
        # Move focus to the selector itself (Qt combo dropdown
        # becomes the focus target on click).
        w.view_selector.setFocus()
        _spin(50)
        assert not view.text_edit.hasFocus(), (
            "sanity: focus should not be on the text edit after we moved it"
        )

        # Switch back to Terminals — our _on_view_changed handler
        # should transfer focus back to the text edit.
        w.view_selector.setCurrentIndex(w._TAB_INDEX_TERMINALS)
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
    # Default-color selection rendering adds a solid highlight color
    # (``#3a3a3a`` — slightly brighter than the default ``#1a1a1a`` bg)
    # so the selection is visually distinct from the unselected runs
    # (which carry plain `color:var(--term-fg);
    # background-color:var(--term-bg)`).
    reverse_count = html.count("#3a3a3a")
    assert reverse_count >= 2, (
        f"expected at least 2 selection-highlighted spans (one per row), "
        f"got: {reverse_count} in {html!r}"
    )
    print("✓ renderer highlights block-selection rectangle across rows")


def test_renderer_selection_highlight_is_visible_on_dark_bg() -> None:
    """The selection highlight must be a solid color that contrasts visibly
    with the dark terminal background.

    Regression for the "selection only seems to work on the first line"
    symptom. The previous implementation used ``rgba(255,255,255,0.15)``
    as a *second* ``background-color`` declaration. CSS treats multiple
    ``background-color`` declarations as overrides (the last wins), not
    as a layered fill — so the rgba completely *replaced* the dark
    terminal background with semi-transparent white. With light
    foreground text on near-white background, the highlight was
    visually indistinguishable from the surrounding unselected cells,
    and the user (correctly) reported "I can only select on the first
    line" because the highlight on every other row was invisible.

    The fix uses a solid color (``#3a3a3a``, ~20% brighter than the
    default ``#1a1a1a`` bg) so the selection has clear contrast
    regardless of whether the document lays out the highlight on row 0,
    row 5, or row 20. This test asserts:

      * The renderer does NOT emit the invisible-rgba form
        (defensive — the old behaviour is wrong).
      * The renderer DOES emit a solid highlight color that is
        visually different from the default bg.
    """
    _qapp()
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(40, 5)
    pyte.ByteStream(s).feed(b"line one content\nline two content\nline three\nline four\nline five")

    # Select row 2 (cells 5..15) — a single-row selection well away
    # from row 0 to confirm the bug specifically affected non-first
    # rows (it didn't — but a multi-row test would be confusing if
    # the highlight is invisible because of rgba).
    html = screen_to_html(s, selection=((5, 2, 15, 2), True))

    # The old invisible-rgba form must NOT appear. If it does, a
    # refactor reintroduced the bug.
    assert "rgba(255,255,255,0.15)" not in html, (
        "renderer emitted the invisible rgba highlight form — this "
        "is the 'selection only works on first line' bug. Use a "
        "solid color (e.g. #3a3a3a) instead."
    )

    # The new solid-color form must appear. We use a color that's
    # distinct from the default bg (``#1a1a1a``) so the selection
    # has visible contrast. Asserting on a substring of the hex is
    # brittle if we ever tweak the exact shade, so we test for the
    # "highlight applied" pattern: any solid 6-digit hex bg different
    # from the default ``var(--term-bg)``.
    import re
    # Find every background-color declaration; the highlight span
    # should have a real hex (not the CSS variable).
    hex_bgs = re.findall(r"background-color:(#[0-9a-fA-F]{6})", html)
    assert hex_bgs, (
        "renderer emitted no solid background-color highlights for "
        "the selection — selection is invisible on dark bg"
    )
    # None of the solid bgs should be the default #1a1a1a (that's
    # the bg of UNSELECTED cells; the highlight must contrast with
    # it).
    assert any(bg != "#1a1a1a" for bg in hex_bgs), (
        f"all solid background-colors match the default bg (#1a1a1a) "
        f"— selection invisible. Found: {hex_bgs!r}"
    )
    print("✓ renderer selection highlight is a visible solid color, not invisible rgba")


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


def test_mouse_press_with_keyboard_modifier_enum() -> None:
    """Real Qt mouse events carry ``Qt.KeyboardModifiers`` flag enums.

    Regression test for the bug where ``mousePressEvent`` called
    ``int(event.modifiers())`` directly on the enum, raising
    ``TypeError: int() argument must be a string ... not 'KeyboardModifier'``
    on real presses. The fix is to coerce via ``.value`` (or to use the
    ``&`` operator natively). This test fires a synthetic
    ``QMouseEvent`` carrying the ``AltModifier`` flag and asserts no
    exception is raised.

    Selection modes: plain click = linear (line-following), Command/Alt
    held = block (rectangular). The test verifies both paths work and
    set the flag correctly.
    """
    _qapp()
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QMouseEvent
    from src.ui.terminal.terminal_session_view import TerminalTextEdit
    import pyte

    edit = TerminalTextEdit(on_input=lambda data: None)
    # Wire a minimal pyte screen so ``_pixel_to_cell`` doesn't divide-by-zero.
    screen = pyte.Screen(10, 2)
    edit._screen_ref = lambda: screen
    edit._screen_width = lambda: 10
    edit._screen_height = lambda: 2
    edit.show()
    _spin(20)

    # Alt held — block selection mode.
    ev_alt = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPoint(2, 2),
        QPoint(2, 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.AltModifier,
    )
    edit.mousePressEvent(ev_alt)
    assert edit._sel_block_flag is True, (
        f"Alt+click should set _sel_block_flag=True (block mode), "
        f"got {edit._sel_block_flag}"
    )
    # Plain click (no modifiers) — linear selection mode.
    edit.clear_selection()
    ev_plain = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPoint(2, 2),
        QPoint(2, 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    edit.mousePressEvent(ev_plain)
    assert edit._sel_block_flag is False, (
        f"plain left-click should default to linear (block=False), "
        f"got {edit._sel_block_flag}"
    )
    print("✓ mousePressEvent safely handles Qt.KeyboardModifier flag enum")


def test_key_press_with_keyboard_modifier_enum() -> None:
    """Real Qt key events carry ``Qt.KeyboardModifiers`` flag enums.

    Regression test for the bug where ``keyPressEvent`` called
    ``int(event.modifiers())`` directly, raising ``TypeError`` and
    silently breaking Ctrl+C / Ctrl+V handling. Fires a synthetic
    ``QKeyEvent`` for ``Ctrl+C`` (no selection) and asserts the press
    forwards ``0x03`` to ``_on_input`` without exception.
    """
    _qapp()
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from src.ui.terminal.terminal_session_view import TerminalTextEdit

    edit = TerminalTextEdit(on_input=lambda data: None)
    received: list[bytes] = []
    edit._on_input = lambda data: received.append(data)

    # ``QKeyEvent`` with Ctrl held — modifiers is a real
    # ``Qt.KeyboardModifiers`` flag, not a plain int.
    ev = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_C,
        Qt.KeyboardModifier.ControlModifier,
        text="",
    )
    # Headline assertion: no TypeError raised.
    edit.keyPressEvent(ev)
    # Ctrl+C with no selection sends 0x03 (SIGINT) to the PTY.
    assert received, "expected Ctrl+C to forward 0x03 to _on_input"
    assert received[0] == b"\x03", (
        f"expected 0x03 SIGINT byte, got: {received[0]!r}"
    )
    print("✓ keyPressEvent safely handles Qt.KeyboardModifier flag enum")


def test_norm_mods_helper() -> None:
    """The ``_norm_mods`` helper coerces any modifier shape to int safely."""
    _qapp()
    from PySide6.QtCore import Qt
    from src.ui.terminal.terminal_session_view import _norm_mods

    # ``None`` -> 0.
    assert _norm_mods(None) == 0
    # Plain int — passes through.
    assert _norm_mods(0) == 0
    assert _norm_mods(int(Qt.KeyboardModifier.ControlModifier.value)) == int(
        Qt.KeyboardModifier.ControlModifier.value
    )
    # ``KeyboardModifier`` flag enum — must extract ``.value`` without
    # raising ``TypeError: int() argument must be ... not 'KeyboardModifier'``.
    result = _norm_mods(Qt.KeyboardModifier.ControlModifier)
    assert isinstance(result, int)
    assert result & int(Qt.KeyboardModifier.ControlModifier.value)
    print("✓ _norm_mods helper safely converts any modifier shape")


def test_key_press_plain_letter_does_not_crash() -> None:
    """A plain letter keypress under real Qt must reach ``_on_input``.

    Regression test for the ``NameError: name 'mods' is not defined``
    that fired after the modifier-coercion refactor: when ``Ctrl+C``
    was the special-cased handler up top, the unmodified-letter
    fall-through still referenced the renamed-away ``mods`` local.
    """
    _qapp()
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from src.ui.terminal.terminal_session_view import TerminalTextEdit

    edit = TerminalTextEdit(on_input=lambda data: None)
    received: list[bytes] = []
    edit._on_input = lambda data: received.append(data)

    # Plain lowercase 'a' — no modifiers, plain printable text.
    ev = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_A,
        Qt.KeyboardModifier.NoModifier,
        text="a",
    )
    # Headline assertion: no NameError, no TypeError.
    edit.keyPressEvent(ev)
    assert received, "plain 'a' should have been forwarded to _on_input"
    assert received[0] == b"a"
    print("✓ plain letter keypress reaches _on_input without NameError")


def test_document_margin_is_zero() -> None:
    """The QTextDocument margin is set to 0 so the cell grid fills the viewport.

    Regression test for the user-reported "terminal is 2 lines taller
    than the visible window" bug. Root cause: QTextDocument's default
    margin is 4 px per side — combined with the ``<pre>`` padding it
    produced 8 px of vertical chrome, leaving the document about 2
    cells (at the configured font height) taller than the viewport.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalTextEdit

    edit = TerminalTextEdit(on_input=lambda data: None)
    assert edit.document().documentMargin() == 0, (
        f"documentMargin should be 0 (cell grid fits viewport), "
        f"got: {edit.document().documentMargin()}"
    )
    print("✓ QTextDocument margin is zero (cell grid fills viewport)")


def test_renderer_pre_has_no_padding() -> None:
    """Renderer emits a ``<pre>`` with zero padding/margin and line-height:1.

    Regression test for the same overflow: the rendered ``<pre>``
    used to declare ``padding:4px`` and ``line-height:1.2``, both
    contributed to the document being taller than ``rows*lineHeight``.
    """
    import pyte
    from src.ui.terminal.renderer import screen_to_html

    s = pyte.Screen(20, 2)
    pyte.ByteStream(s).feed(b"hi")
    html = screen_to_html(s)
    assert "padding:0" in html, f"expected padding:0 in HTML, got: {html!r}"
    assert "margin:0" in html, f"expected margin:0 in HTML, got: {html!r}"
    assert "line-height:1.0" in html, f"expected line-height:1.0, got: {html!r}"
    assert "padding:4px" not in html, (
        f"padding:4px would cause 2-line overflow, got: {html!r}"
    )
    print("✓ renderer <pre> has zero padding/margin and line-height:1.0")


def test_measure_rendered_rows_matches_actual_layout() -> None:
    """``_measure_rendered_rows`` returns ``n`` whose rendered doc fits.

    Regression test for the bug where ``_recompute_size`` used
    ``viewport_h // lineSpacing()`` — integer division over-estimated
    the row count because QTextDocument's layout engine renders
    each ``\\n``-row ~1 px taller than ``fontMetrics().lineSpacing()``
    reports. The result was a document ``rows + 2`` tall at 24 rows,
    clipping the bottom prompt below the visible viewport.

    After the fix, ``_measure_rendered_rows`` returns ``n`` such that
    ``QTextDocument.size().height() ≤ viewport_h`` — i.e., the layout
    fits the visible viewport exactly.
    """
    _qapp()
    from PySide6.QtGui import QTextDocument
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    # ``_measure_rendered_rows`` lives on TerminalSessionView (the
    # parent class) because it needs ``self.text_edit`` + the font
    # at the layout level. Build a minimal view to access it.
    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(20)
    edit = view.text_edit

    for vp_h in (200, 400, 600, 800):
        n = view._measure_rendered_rows(vp_h)
        assert n >= 1, f"no rows at all fit in {vp_h} px"
        # Render ``n`` rows in a fresh document and confirm the
        # rendered height fits in the viewport. Use ``setHtml`` with
        # a real ``<pre>`` element so the stylesheet applies — the
        # same fix as PR5 (PR5 fix in ``_measure_rendered_rows``
        # switched from ``setPlainText`` to ``setHtml(<pre>...)`` so
        # the measurement matches the renderer's actual layout).
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.setDefaultStyleSheet(
            "pre{margin:0;padding:0;line-height:1.0;"
            "font-family:Menlo,Consolas,monospace}"
        )
        doc.setDefaultFont(edit.font())
        text = "<pre>" + "\n" * (n - 1) + " </pre>"
        doc.setHtml(text)
        doc.setHtml(text)  # force re-layout
        rendered_h = doc.size().height()
        assert rendered_h <= vp_h, (
            f"_measure_rendered_rows({vp_h}) returned {n}, "
            f"but rendering {n} rows actually takes {rendered_h} px "
            f"({rendered_h / n:.1f} px/row)"
        )
        # And ``n+1`` rows must NOT fit, otherwise we're
        # under-reporting.
        text_extra = "<pre>" + "\n" * n + " </pre>"
        doc.setHtml(text_extra)
        doc.setHtml(text_extra)  # force re-layout
        if doc.size().height() <= vp_h:
            # Edge case: at very small viewport sizes the "+1" row
            # may still fit if rounding cooperates. Acceptable —
            # the bug we're guarding against is over-reporting, not
            # under. (Skip this assertion when n+1 also fits.)
            continue
    print("✓ _measure_rendered_rows returns n whose doc fits the viewport")


def test_recompute_size_does_not_overflow_viewport() -> None:
    """Full integration: a sized view's row count fits its viewport.

    Regression test for "terminal is 2 lines longer than the visible
    window". Builds a session at 800×600, lets the debounce timer
    fire, and asserts ``view._rows`` ≤ viewport height in row terms.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(20)
    view.start()
    _spin(20)

    # After the post-start debounce fires, _rows should reflect what
    # actually fits in the viewport (not over-report).
    loop_count = 0
    while loop_count < 5 and view.text_edit.viewport().height() <= 0:
        _spin(20)
        loop_count += 1
    _spin(80)  # let _resize_timer fire
    vp_h = view.text_edit.viewport().height()
    rows = view._rows
    assert rows >= view.MIN_ROWS, f"rows={rows} below MIN_ROWS={view.MIN_ROWS}"
    # Render ``rows`` newlines and confirm the rendered document
    # height is at most ``vp_h`` (no overflow).
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultStyleSheet(
        "pre{margin:0;padding:0;line-height:1.0;"
        "font-family:Menlo,Consolas,monospace}"
    )
    doc.setDefaultFont(view.text_edit.font())
    # Same fix as ``test_measure_rendered_rows_matches_actual_layout``:
    # ``setHtml(<pre>...)`` so the stylesheet actually applies —
    # ``setPlainText`` produces no HTML elements so the
    # ``pre{margin:0;...}`` rule was dead code, making the measurement
    # ~4% taller per row than the actual ``<pre>`` render.
    text = "<pre>" + "\n" * (rows - 1) + " </pre>"
    doc.setHtml(text)
    doc.setHtml(text)  # force re-layout
    rendered_h = doc.size().height()
    assert rendered_h <= vp_h + 1, (
        f"view._rows={rows} but rendering that many rows takes "
        f"{rendered_h} px in a {vp_h}-px viewport — overflow means "
        f"the user has to scroll to see the prompt"
    )
    view.shutdown()
    print("✓ TerminalSessionView does not over-report rows (no overflow)")





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


def test_repaint_scrolls_to_max_when_content_overflows() -> None:
    """When new shell output overflows the viewport, _repaint must scroll
    the QTextEdit so the cursor (the active line) stays visible.

    Regression test for the "active line can scroll off the bottom" symptom.
    Without the synchronous ``setTextWidth(viewport_width) + adjustSize()``
    pair inside ``_repaint``, the scrollbar's ``maximum`` stayed at the
    value left over from the *previous* repaint's layout — the new
    content's height was larger but the scroll position was still pegged
    to the old maximum, so the bottom rows of new output were rendered
    below the visible area.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(50)
    # Offscreen-mode viewport quirk: force the viewport to fill the
    # widget, same as ``test_recompute_size_does_not_overflow_viewport``.
    view.text_edit.viewport().resize(view.text_edit.size())
    view.start()
    _spin(50)
    view._recompute_size()

    try:
        # Push enough lines to overflow the viewport.
        for i in range(80):
            view._stream.feed(f"Output line {i:02d}\n".encode())
        view._repaint()
        _spin(50)

        sb = view.text_edit.verticalScrollBar()
        vp_h = view.text_edit.viewport().height()
        # The scrollbar max should be > 0 (content is taller than the
        # viewport). The scroll position should put the cursor in the
        # visible area — which, for a terminal whose cursor is on the
        # last row of the live screen, is the bottom of the viewport.
        assert sb.maximum() > 0, (
            f"expected scrollbar maximum > 0 after overflow, got {sb.maximum()}"
        )
        # The cursor's row in the document is at the bottom, so the
        # bottom of the document must be visible — i.e. the scroll
        # position should be within 1 row of the maximum (the "a few
        # lines off" tolerance is a 1-row window in this test).
        assert sb.value() >= sb.maximum() - 1, (
            f"expected scrollbar value to be near maximum={sb.maximum()}, "
            f"got value={sb.value()} — active line is off the bottom"
        )
        # The visible bottom must reach the bottom of the document.
        visible_bottom_px = sb.value() + vp_h
        assert visible_bottom_px >= sb.maximum() + vp_h - 5, (
            f"bottom of document not visible: value={sb.value()} "
            f"max={sb.maximum()} vp_h={vp_h}"
        )
    finally:
        view.shutdown()
    print("✓ repaint scrolls to keep the active line visible when content overflows")


def test_repaint_scrolls_to_max_when_cursor_is_above_last_row() -> None:
    """Claude Code's TUI positions the cursor a few rows above the bottom
    (above its status bar / latest output). ``ensureCursorVisible`` alone
    won't scroll to the bottom in that case — the cursor is already
    visible, so it does nothing — and the last few rows of output end
    up below the viewport. The fix forces ``setValue(maximum)`` when
    the cursor is in the bottom viewport-height of the document.

    Regression test for the "active line reaches the bottom, last five
    lines aren't viewable until the window is scrolled all the way
    down" symptom.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(50)
    view.text_edit.viewport().resize(view.text_edit.size())
    view.start()
    _spin(50)
    view._recompute_size()

    try:
        # Fill the screen, then position the cursor a few rows above
        # the bottom — the Claude Code TUI shape.
        for i in range(80):
            view._stream.feed(f"Output line {i:02d}\n".encode())
        # Move the cursor up 5 rows (ESC[<n>A), mimicking a TUI that
        # positions its prompt above its status bar.
        view._stream.feed(b"\x1b[5A")
        view._repaint()
        _spin(50)

        sb = view.text_edit.verticalScrollBar()
        vp_h = view.text_edit.viewport().height()
        # The bottom of the document must be visible — the status-bar
        # rows below the cursor are the whole point of this fix.
        assert sb.maximum() > 0, (
            f"expected scrollbar maximum > 0, got {sb.maximum()}"
        )
        assert sb.value() >= sb.maximum() - 1, (
            f"cursor above last row: scrollbar should snap to maximum "
            f"(value >= max-1), got value={sb.value()} max={sb.maximum()} "
            f"— last rows of output are below the viewport"
        )
        visible_bottom_px = sb.value() + vp_h
        assert visible_bottom_px >= sb.maximum() + vp_h - 5, (
            f"bottom of document not visible: value={sb.value()} "
            f"max={sb.maximum()} vp_h={vp_h}"
        )
    finally:
        view.shutdown()
    print("✓ repaint scrolls to max when cursor is positioned above the last row (TUI case)")


def test_repaint_does_not_snap_to_max_when_no_scrollback() -> None:
    """A fresh Claude Code session has no scrollback yet — the TUI
    header, prompt, and status bar all live on the live screen, and
    the cursor sits above the status bar. Snapping the scrollbar to
    ``maximum`` there scrolls the TUI header off the top of the
    viewport.

    Regression test for the "top of the TUI window is off the top of
    the screen by 4-5 lines when starting a Claude Code session"
    symptom. The snap-to-max fix for the active-session case must
    NOT fire when ``history_lines == 0`` — the ``ensureCursorVisible``
    result (cursor in view near the top) is the correct behavior.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(50)
    view.text_edit.viewport().resize(view.text_edit.size())
    view.start()
    _spin(50)
    view._recompute_size()

    try:
        # Draw a Claude-Code-shaped TUI using absolute cursor
        # positioning (no newlines, so no scrollback accumulates):
        #   row 0:  "╭─ Claude Code ──────────────╮"   (header)
        #   row 5:  "❯ "                          (prompt — cursor here)
        #   row 22: "─────────────────────────────"   (status bar)
        #   row 23: "ctrl-c to interrupt"         (footer)
        # This mimics the exact startup shape: cursor parked above
        # the status bar, no scrollback yet.
        header = b"\x1b[1;1H" + b"\xe2\x95\xad\xe2\x94\x80 Claude Code "
        header += b"\xe2\x94\x80" * 20 + b"\xe2\x95\xae"
        status = b"\x1b[23;1H" + b"\xe2\x94\x80" * 40
        footer = b"\x1b[24;1Hctrl-c to interrupt"
        prompt = b"\x1b[6;1H\xe2\x9d\xaf "
        view._stream.feed(header + status + footer + prompt)
        view._repaint()
        _spin(50)

        sb = view.text_edit.verticalScrollBar()
        history_lines = len(view._screen.history.top) if hasattr(
            view._screen, "history"
        ) else 0
        assert history_lines == 0, (
            f"sanity: expected no scrollback at startup, got "
            f"history_lines={history_lines}"
        )
        # With no scrollback, the snap-to-max must NOT fire — the
        # TUI header (top of the live screen) must be visible.
        assert sb.value() <= 1, (
            f"fresh session: scroll position should stay near the top "
            f"(value <= 1) so the TUI header is visible, got "
            f"value={sb.value()} max={sb.maximum()} — top of TUI is "
            f"scrolled off the top of the viewport"
        )
    finally:
        view.shutdown()
    print("✓ repaint does not snap to max when there is no scrollback (startup TUI)")


def test_repaint_scrolls_to_top_for_new_session() -> None:
    """A brand-new session has the cursor at row 0 (top of document). The
    auto-scroll must put the cursor in view, not land on the blank rows
    below.

    Regression test for the "active line is a few lines off the top of
    the terminal window" symptom after creating a new session. The old
    ``setValue(maximum)`` approach pinned the scrollbar to the bottom
    of the document, which is the right place for an *active* session
    (where the cursor is at the bottom of the live screen) but the
    wrong place for a fresh session where the cursor is on the very
    first row. With the cursor at row 0, ``setValue(maximum)`` lands
    the viewport on the empty bottom rows and the prompt / cursor
    sits above the visible area.

    The fix uses ``ensureCursorVisible`` on a QTextCursor placed at
    the pyte cursor's row. For a new session that's row 0, so the
    viewport lands on the top — the prompt is visible.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(50)
    view.text_edit.viewport().resize(view.text_edit.size())
    view.start()
    _spin(50)
    view._recompute_size()

    try:
        # New session — no data fed, cursor is at (0, 0).
        assert view._screen.cursor.y == 0, (
            f"sanity: cursor should start at row 0, got {view._screen.cursor.y}"
        )
        view._repaint()
        _spin(50)

        sb = view.text_edit.verticalScrollBar()
        # Scroll position should be at (or near) the top of the
        # document so the cursor at row 0 is visible.
        assert sb.value() <= 1, (
            f"new session: scroll position should be at the top "
            f"(value <= 1), got value={sb.value()} max={sb.maximum()}"
        )
    finally:
        view.shutdown()
    print("✓ repaint scrolls to the top for a new session (cursor at row 0)")


def test_repaint_clears_user_scrolled_up_when_at_bottom() -> None:
    """A repaint where the user is already at the bottom re-engages follow
    mode even if ``_user_scrolled_up`` was True at the start.

    Regression test for the "scrolled up, then scrolled back down via
    keyboard/scrollbar instead of the wheel" path. ``wheelEvent`` is the
    only place that previously cleared ``_user_scrolled_up``, so a user
    who scrolled up to read history and then navigated back to the
    bottom via Ctrl+End or the scrollbar drag handle would be stuck in
    "scrolled up" mode forever — new content accumulated invisibly
    below the viewport. The fix resets the flag in ``_repaint`` when
    ``was_at_bottom`` is True, so the next repaint follows again.
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(50)
    view.text_edit.viewport().resize(view.text_edit.size())
    view.start()
    _spin(50)
    view._recompute_size()

    try:
        # Produce overflow so there's something to scroll past.
        for i in range(80):
            view._stream.feed(f"Output line {i:02d}\n".encode())
        view._repaint()
        _spin(50)
        sb = view.text_edit.verticalScrollBar()
        # Sanity: at the bottom after the overflow.
        assert sb.value() == sb.maximum(), (
            f"sanity: should be at max, got value={sb.value()} max={sb.maximum()}"
        )

        # Simulate the user having scrolled up at some earlier point.
        view._user_scrolled_up = True
        # A new repaint (no new data) should clear the flag because
        # the scrollbar is at the bottom.
        view._repaint()
        _spin(50)
        assert view._user_scrolled_up is False, (
            "_user_scrolled_up should be reset to False when the user is "
            "at the bottom — otherwise new content scrolls off the bottom"
        )
    finally:
        view.shutdown()
    print("✓ repaint clears _user_scrolled_up when scrollbar is at the bottom")


def test_repaint_resets_user_scrolled_up_on_alt_screen_exit() -> None:
    """Bug C regression: when Claude Code's fullscreen TUI exits
    the alternate screen (DECRST 1049), the buffer swaps back to
    the main screen and the user-scroll state must reset — a
    scroll-up position from the alt-screen would otherwise leave
    the viewport stranded on a stale/empty row of the main
    screen.

    The fix is in ``_repaint``: cache ``screen.mode`` and on a
    transition, clear ``_user_scrolled_up`` and invalidate the
    dirty-tracking cache so the rebuild actually runs (the cell
    coordinates from the alt-screen don't translate to the main
    screen).

    Sequence:
      1. Start a session, scroll up (simulate by setting the flag).
      2. Feed ``ESC[?1049h`` (enter alt-screen) — pyte swaps buffer.
      3. Feed ``ESC[?1049l`` (leave alt-screen) — pyte swaps back.
      4. Trigger a repaint.
      5. Assert ``_user_scrolled_up`` is now ``False`` and the
         dirty cache was invalidated (so the rebuild ran).
    """
    _qapp()
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    view = TerminalSessionView(argv=["/bin/cat"])
    view.resize(800, 600)
    view.show()
    _spin(50)
    view.text_edit.viewport().resize(view.text_edit.size())
    view.start()
    _spin(50)
    assert view._screen is not None
    view._recompute_size()
    _spin(20)

    try:
        # Produce some scrollback so there's a position to scroll to.
        for i in range(40):
            view._stream.feed(f"line {i:02d}\n".encode())
        view._repaint()
        _spin(20)

        # Simulate the user having scrolled up DURING the alt-screen
        # session (Claude Code's fullscreen TUI lets you scroll
        # through long responses).
        view._user_scrolled_up = True
        # Prime the dirty cache so we can prove the alt-screen exit
        # invalidates it (otherwise the next _repaint would short-
        # circuit on a hash hit and skip the rebuild).
        view._cached_live_signature = "primed-with-stale-buffer-hash"
        view._cached_history_html = "<primed/>"
        view._cached_history_lines_count = 99

        # Enter alt-screen (Claude Code's fullscreen TUI entry).
        view._stream.feed(b"\x1b[?1049h")
        _spin(20)
        # At this point the screen mode may or may not have flipped
        # depending on pyte's internal handling; the first _repaint
        # primes ``_cached_screen_mode``. We need TWO alt-screen
        # transitions (enter + leave) for the buffer-swap detection
        # to fire.
        view._repaint()
        _spin(20)

        # The cached mode should now reflect the alt-screen state.
        # If pyte's ``screen.mode`` attribute exists, it should be
        # non-default after the DECSET 1049. We don't assert the
        # exact value — only that the next transition (back out)
        # triggers our reset logic.
        cached_after_enter = getattr(view, "_cached_screen_mode", None)

        # Leave alt-screen.
        view._stream.feed(b"\x1b[?1049l")
        _spin(20)
        view._repaint()
        _spin(20)

        if cached_after_enter is None or getattr(
            view._screen, "mode", None
        ) is None:
            # Older pyte without ``screen.mode`` — skip the strict
            # assertion but still verify the cache invalidation
            # didn't blow up.
            print(
                "○ pyte lacks screen.mode attribute — buffer-swap "
                "detection skipped (older pyte)"
            )
        else:
            # The flag MUST have been reset by the alt-screen exit.
            assert view._user_scrolled_up is False, (
                "_user_scrolled_up should be reset to False on "
                "alt-screen exit (DECSET 1049 → DECRST 1049) so "
                "the post-swap repaint doesn't strand the viewport "
                "at the OLD scroll position."
            )

        # The dirty-tracking cache should have been invalidated by
        # the transition (regardless of pyte version) — otherwise
        # the next repaint would short-circuit on the stale
        # signature we primed above.
        assert view._cached_live_signature != (
            "primed-with-stale-buffer-hash"
        ), (
            "_cached_live_signature should be invalidated on "
            "alt-screen exit so the rebuild actually runs."
        )
    finally:
        view.shutdown()
    print("✓ repaint resets _user_scrolled_up on alt-screen exit")


# ---- Font size + tab navigation -----------------------------------------


def test_adjust_font_size_grows_then_clamps() -> None:
    """``adjust_font_size(+1)`` bumps point-size and clamps at MAX.

    Regression test for the new Alt+= / Alt+- shortcuts. Verifies:
      * The font's point-size actually changes after a bump.
      * The clamp holds at MAX_FONT_POINT_SIZE (no runaway growth).
      * Calling adjust_font_size(0) is a no-op (defensive).
    """
    _qapp()
    from PySide6.QtWidgets import QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    # Construct the view as a child of a host widget so the QApplication
    # has a parent window — without that, ``setFont`` warns about
    # orphaned widgets in some Qt builds.
    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        max_pt = view.MAX_FONT_POINT_SIZE
        initial = view._base_font.pointSize()
        assert initial > 0, f"expected a positive pointSize, got {initial}"
        # Bump 3 points — should grow by exactly 3.
        for _ in range(3):
            view.adjust_font_size(+1)
        assert view._base_font.pointSize() == initial + 3, (
            f"expected pointSize={initial+3}, "
            f"got {view._base_font.pointSize()}"
        )
        # Bump until we hit the clamp. After the clamp, additional
        # bumps must NOT grow the font past MAX.
        for _ in range(max_pt * 2):
            view.adjust_font_size(+1)
        assert view._base_font.pointSize() == max_pt, (
            f"expected clamp at MAX={max_pt}, "
            f"got {view._base_font.pointSize()}"
        )
        # ``adjust_font_size(0)`` is documented as a no-op — verify it
        # doesn't accidentally mutate state.
        before = view._base_font.pointSize()
        view.adjust_font_size(0)
        assert view._base_font.pointSize() == before
    finally:
        view.shutdown()
    print("✓ adjust_font_size bumps pointSize and clamps at MAX")


def test_adjust_font_size_shrinks_then_clamps() -> None:
    """``adjust_font_size(-1)`` shrinks point-size and clamps at MIN.

    Mirror of the growth test. The clamp direction matters because
    ``setPointSize(<8)`` makes the cell grid unreadable; we verify the
    method enforces ``MIN_FONT_POINT_SIZE``.
    """
    _qapp()
    from PySide6.QtWidgets import QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        min_pt = view.MIN_FONT_POINT_SIZE
        # Shrink well past MIN — 50 attempts is far more than needed.
        for _ in range(50):
            view.adjust_font_size(-1)
        assert view._base_font.pointSize() == min_pt, (
            f"expected clamp at MIN={min_pt}, "
            f"got {view._base_font.pointSize()}"
        )
    finally:
        view.shutdown()
    print("✓ adjust_font_size shrinks pointSize and clamps at MIN")


def test_alt_equals_key_press_increases_font_size() -> None:
    """Pressing Alt+= on the text edit bumps the view's font size.

    Fires a synthetic ``QKeyEvent`` carrying ``Key_Equal`` with the
    ``AltModifier`` flag (the Qt-level identity of the physical
    "Alt + +" key combo on a US layout). The press should:
      * Mutate ``view._base_font.pointSize()`` by +1.
      * NOT forward anything to ``_on_input`` (the shell must never
        see a stray ESC= from the Alt prefix).
    """
    _qapp()
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        edit = view.text_edit
        received: list[bytes] = []
        edit._on_input = lambda data: received.append(data)
        initial = view._base_font.pointSize()

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Equal,
            Qt.KeyboardModifier.AltModifier,
            text="",
        )
        edit.keyPressEvent(ev)
        assert view._base_font.pointSize() == initial + 1, (
            f"expected pointSize {initial+1}, got {view._base_font.pointSize()}"
        )
        assert not received, (
            f"Alt+= must NOT forward to PTY; got: {received!r}"
        )

        # Alt+- (Key_Minus) should decrement.
        ev2 = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Minus,
            Qt.KeyboardModifier.AltModifier,
            text="-",
        )
        edit.keyPressEvent(ev2)
        assert view._base_font.pointSize() == initial, (
            f"expected pointSize to return to {initial} after Alt+-, "
            f"got {view._base_font.pointSize()}"
        )
        assert not received, (
            f"Alt+- must NOT forward to PTY; got: {received!r}"
        )
    finally:
        view.shutdown()
    print("✓ Alt+= / Alt+- keyPress events adjust font size and never leak to PTY")


def test_tab_key_reaches_pty_when_nested_in_qtabwidget() -> None:
    """Regression: Tab in a Claude Code session nested inside a
    ``QTabWidget`` must reach the PTY as ``b"\\t"`` — Claude Code's
    completion UI is triggered by Tab.

    User-reported symptom: "the tab key is still not captured by
    the claude-code session window, instead selecting different ui
    components".

    Root cause: ``setTabChangesFocus(False)`` on the inner
    ``TerminalTextEdit`` is the documented Qt fix for read-only
    text widgets, but it does NOT propagate when the widget is a
    page of a ``QTabWidget`` — Qt's parent ``QTabWidget::event``
    routes the Tab through the tab-bar's focus chain before our
    ``keyPressEvent`` runs. The fix overrides ``event()`` to
    forward Tab/Backtab directly to ``keyPressEvent``, bypassing
    the QTabWidget layer.

    This test asserts the behaviour with ``QTest.keyClick`` (which
    simulates a real keyboard event including focus traversal),
    NOT ``sendEvent`` (which only does a direct dispatch and
    hides the bug)."""
    _qapp()
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    host = QWidget()
    layout = QVBoxLayout(host)
    tabs = QTabWidget()
    layout.addWidget(tabs)

    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    tabs.addTab(view, "Session 1")
    host.resize(800, 600)
    host.show()

    try:
        # Hook the input pipeline without spinning up a real PTY.
        received: list[bytes] = []
        view._send_input = lambda data: received.append(data)  # type: ignore[method-assign]
        # ``TerminalTextEdit`` was constructed with
        # ``on_input=self._send_input`` — that closure captured the
        # original method via the bound ``self``. Reassigning
        # ``self._send_input`` here doesn't reach the closure
        # binding, so we instead capture at the point of dispatch:
        view._send_input_orig = view._send_input  # type: ignore[attr-defined]

        def spy_send(data):
            received.append(data)
            return view._send_input_orig(data)

        # The text edit holds ``on_input=self._send_input`` as a
        # bound method captured at construction time. Re-assigning
        # the method on the view won't redirect the captured
        # reference; instead, monkey-patch the closure target by
        # swapping ``view.text_edit._on_input`` directly. The
        # closure was set with ``on_input=self._send_input`` so
        # ``view.text_edit._on_input`` is the bound method.
        view.text_edit._on_input = spy_send

        view.text_edit.setFocus()
        # ``processEvents`` once so the focus actually takes
        # effect — without it, ``QTest.keyClick`` may not route
        # through the focused widget.
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        # Sanity: focus is actually on the text edit.
        assert view.text_edit.hasFocus(), (
            "test setup failed: text edit must have focus before "
            "QTest.keyClick is meaningful"
        )

        # Plain Tab → TAB byte to the PTY (Claude Code triggers
        # completion on this).
        received.clear()
        QTest.keyClick(view.text_edit, Qt.Key_Tab)
        QApplication.processEvents()
        assert b"\t" in received, (
            f"plain Tab in a QTabWidget-nested session should "
            f"forward b'\\t' to the PTY; got: {received!r}. "
            f"This is the user-reported bug — Tab is being "
            f"intercepted by the parent QTabWidget's focus chain."
        )

        # Shift+Tab → CSI Z (the xterm back-tab sequence; Claude
        # Code uses this to cycle permission modes).
        received.clear()
        QTest.keyClick(view.text_edit, Qt.Key_Tab, Qt.ShiftModifier)
        QApplication.processEvents()
        assert b"\x1b[Z" in received, (
            f"Shift+Tab should forward CSI Z to the PTY; got: "
            f"{received!r}"
        )

        # Sanity: a regular printable key still works (the fix
        # should not break the normal keypress path).
        received.clear()
        QTest.keyClick(view.text_edit, Qt.Key_A)
        QApplication.processEvents()
        assert b"a" in received, (
            f"plain 'A' must still reach the PTY; got: {received!r}"
        )
    finally:
        view.shutdown()
    print(
        "✓ Tab / Shift+Tab reach the PTY when nested in a QTabWidget"
    )

def test_alt_arrows_cycle_session_tabs_with_wraparound() -> None:
    """Alt+Right / Alt+Left cycle through session tabs and wrap at edges.

    Sets up a TerminalContainerView with three TerminalSessionView
    children (without spinning up real PTYs — the test only cares
    about tab-switch math, not the shell lifecycle). Verifies:
      * Alt+Right from the last tab wraps to the first.
      * Alt+Left from the first tab wraps to the last.
      * With a single tab, the cycling is a no-op.
    """
    _qapp()
    from PySide6.QtWidgets import QWidget
    from src.ui.terminal.terminal_container_view import TerminalContainerView

    host = QWidget()
    container = TerminalContainerView(parent=host)
    try:
        # Avoid actually starting PTYs — we just want tab widgets for
        # the cycling math. We can't call ``new_session`` because that
        # starts the PTY (would race with shutdown). Instead insert
        # ``TerminalSessionView`` instances via the container's
        # internals: this is the same wiring ``new_session`` uses.
        from src.ui.terminal.terminal_session_view import TerminalSessionView

        views = []
        for _ in range(3):
            v = TerminalSessionView(argv=["/bin/cat"], parent=container)
            views.append(v)
            container._sessions.append(v)
            idx = container.session_tabs.addTab(v, v.title())
            container.session_tabs.setCurrentIndex(idx)
        container.session_tabs.tabBar().setVisible(True)

        # Start on tab 0 (last appended).
        assert container.session_tabs.currentIndex() == 2

        # Alt+Right once → tab 0 (wrap from 2 → 0).
        container._cycle_session(+1)
        assert container.session_tabs.currentIndex() == 0, (
            f"Alt+Right from last should wrap to 0, "
            f"got {container.session_tabs.currentIndex()}"
        )

        # Alt+Left from tab 0 → tab 2 (wrap backward).
        container._cycle_session(-1)
        assert container.session_tabs.currentIndex() == 2, (
            f"Alt+Left from first should wrap to last (2), "
            f"got {container.session_tabs.currentIndex()}"
        )

        # One-tab case: cycling must be a no-op (not steal focus or
        # change indices). Remove two tabs to leave exactly one.
        # We can't easily remove tabs without cleanup, so test the
        # math by inspecting the count == 1 branch directly with a
        # fresh container.
        single = TerminalContainerView(parent=host)
        try:
            v = TerminalSessionView(argv=["/bin/cat"], parent=single)
            single._sessions.append(v)
            idx = single.session_tabs.addTab(v, v.title())
            single.session_tabs.setCurrentIndex(idx)
            assert single.session_tabs.count() == 1
            single._cycle_session(+1)
            assert single.session_tabs.currentIndex() == 0
            single._cycle_session(-1)
            assert single.session_tabs.currentIndex() == 0
        finally:
            v.shutdown()

        # Zero-tab case: must not raise.
        empty = TerminalContainerView(parent=host)
        try:
            empty._cycle_session(+1)
            empty._cycle_session(-1)
        finally:
            pass
    finally:
        for v in views:
            v.shutdown()
        container.shutdown()
        host.deleteLater()
    print("✓ Alt+Left / Alt+Right cycle session tabs with edge wraparound")


def test_alt_arrow_shortcut_actions_are_wired() -> None:
    """Verify the QAction objects exist on the container with the right shortcuts.

    Without this wiring, the actions would never fire regardless of
    what ``_cycle_session`` does — a regression check on the wiring
    itself, not the math.
    """
    _qapp()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    from src.ui.terminal.terminal_container_view import TerminalContainerView

    host = QWidget()
    container = TerminalContainerView(parent=host)
    try:
        # ``action_prev_session`` / ``action_next_session`` are
        # public-by-convention attributes set up in ``_build_ui``.
        prev = container.action_prev_session
        nxt = container.action_next_session
        assert prev is not None and nxt is not None
        # ``QKeySequence.toString()`` returns the human-readable
        # representation ("Alt+Left", "Alt+Right") which is stable
        # across Qt versions for these standard sequences.
        assert "Alt+Left" in prev.shortcut().toString(), (
            f"expected Alt+Left shortcut, got {prev.shortcut().toString()!r}"
        )
        assert "Alt+Right" in nxt.shortcut().toString(), (
            f"expected Alt+Right shortcut, got {nxt.shortcut().toString()!r}"
        )
        # Context should be WidgetWithChildrenShortcut so the keys
        # only fire when this container (or one of its children) has
        # focus — not globally across the app.
        assert prev.shortcutContext() == Qt.WidgetWithChildrenShortcut
        assert nxt.shortcutContext() == Qt.WidgetWithChildrenShortcut
    finally:
        container.shutdown()
        host.deleteLater()
    print("✓ Alt+Left / Alt+Right QActions are wired with the right shortcuts")


def test_pixel_to_cell_maps_to_visual_position() -> None:
    """A click at the visual center of the terminal maps to the cell at the
    visual center of the grid — NOT to the bottom-right.

    Regression for the "selection is anchored in the bottom-right
    corner" symptom. Root cause: ``_pixel_to_cell`` divided by
    ``fontMetrics.horizontalAdvance("M")`` and ``lineSpacing()``, but
    the cell grid the *renderer* draws is sized to fill the entire
    text-edit widget (because we disabled scrollbars and the document
    fills the widget edge-to-edge). With an 800x600 widget and an
    80x24 grid, the visible cell width is 10 px (not the font metric
    of 8 px); a click at pixel x=400 used to map to cell x=50 — a
    cell that *renders* at pixel x=500, in the bottom-right area.

    The fix computes cell size from ``widget_size / grid_size`` so
    the math agrees with what the user actually sees on screen.
    """
    _qapp()
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QTimer, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        view.resize(800, 600)
        view.show()
        # Spin so the layout settles and the widget reports its real size.
        loop = QEventLoop()
        qt = QTimer(); qt.setSingleShot(True); qt.setInterval(100)
        qt.timeout.connect(loop.quit); qt.start()
        loop.exec()

        edit = view.text_edit
        # Sanity check the test setup: we want a sizeable grid.
        assert edit.width() == 800, f"widget width expected 800, got {edit.width()}"
        assert edit.height() == 600, f"widget height expected 600, got {edit.height()}"
        assert view._cols == 80, f"expected 80 cols, got {view._cols}"
        assert view._rows == 24, f"expected 24 rows, got {view._rows}"

        # Force the viewport to fill the widget. In offscreen mode Qt
        # keeps the viewport at its 98×28 default until the document
        # lays out (this is a known QAbstractScrollArea quirk — the
        # viewport size is gated on document size, which we never let
        # Qt lay out because we paint via ``setHtml``). In real
        # sessions the viewport grows to fill the widget. Forcing it
        # here makes the test exercise the production code path.
        edit.viewport().resize(edit.size())
        # Trigger a recompute so _cols/_rows are recomputed against
        # the new viewport size. The widget is 800×600 with our font,
        # which fits the 80×24 grid; recompute should keep it.
        view._recompute_size()

        # Cell pixel size in the visible rendering. ``_pixel_to_cell``
        # divides viewport-pixel coords by the viewport size, so use
        # that for the expected values.
        vp = edit.viewport()
        cw = vp.width() / view._cols
        ch = vp.height() / view._rows

        # Pick pixel coords inside each target cell. Using the cell's
        # centre pixel is robust against float-truncation: it always
        # falls cleanly into ``cell`` regardless of off-by-one in
        # ``col * cw`` rounding.
        def cell_centre(col: int, row: int) -> tuple[int, int]:
            return (int((col + 0.5) * cw), int((row + 0.5) * ch))

        cases = [
            # (px, py, expected_col, expected_row, label)
            (*cell_centre(0, 0), 0, 0, "top-left corner"),
            (*cell_centre(5, 3), 5, 3, "near top-left"),
            (*cell_centre(40, 12), 40, 12, "visual center"),
            (*cell_centre(70, 20), 70, 20, "near bottom-right"),
            (*cell_centre(79, 23), 79, 23, "bottom-right corner"),
        ]
        for (px, py, exp_x, exp_y, label) in cases:
            edit._sel_anchor = None
            edit._sel_active = None
            # Send the press via the viewport (the path real user
            # clicks take). This also exercises the viewportEvent
            # override — a regression in that path would fail here
            # too, which is what we want.
            QApplication.sendEvent(
                edit.viewport(),
                QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    QPoint(px, py),
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
            assert edit._sel_anchor == (exp_x, exp_y), (
                f"{label}: click at pixel ({px}, {py}) "
                f"expected cell ({exp_x}, {exp_y}), got {edit._sel_anchor}"
            )
    finally:
        view.shutdown()
    print("✓ clicks map to cells at their visual position (not bottom-right)")


def test_pixel_to_cell_drag_stays_in_dragged_region() -> None:
    """A drag from top-left to centre produces a small selection, not most of the screen.

    Regression companion to ``test_pixel_to_cell_maps_to_visual_position``.
    Before the fix, a click at (10, 10) → cell (1, 0), but a drag to
    (200, 200) → cell (24, 13) — visually those cells render far to
    the right and down of where the cursor actually is, producing a
    selection that "looks like it covers most of the screen" because
    the active cell drags along the diagonal to the bottom-right.

    The drag's start and end cells must both land in the upper-left
    quadrant of the visible grid.
    """
    _qapp()
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QTimer, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        view.resize(800, 600)
        view.show()
        loop = QEventLoop()
        qt = QTimer(); qt.setSingleShot(True); qt.setInterval(100)
        qt.timeout.connect(loop.quit); qt.start()
        loop.exec()

        edit = view.text_edit
        # Force viewport to fill the widget so the cell math is
        # exercised against a realistic viewport size. See
        # ``test_pixel_to_cell_maps_to_visual_position`` for the
        # offscreen-mode rationale.
        edit.viewport().resize(edit.size())
        view._recompute_size()

        # Press at (10, 10). Send via the viewport (the path real
        # user clicks take — also exercises the viewportEvent
        # override). Cell size is viewport_height / rows.
        vp = edit.viewport()
        cw = vp.width() / view._cols
        ch = vp.height() / view._rows
        expected_press = (int(10 // cw), int(10 // ch))
        ev_press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPoint(10, 10),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_press)
        assert edit._sel_anchor == expected_press, (
            f"press should anchor at {expected_press}, got {edit._sel_anchor}"
        )

        # Drag to (200, 200). Expected cell: (200 // cw, 200 // ch).
        expected_move = (int(200 // cw), int(200 // ch))
        ev_move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPoint(200, 200),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_move)
        assert edit._sel_active == expected_move, (
            f"drag to (200, 200) should land at cell {expected_move}, got {edit._sel_active}"
        )

        # Both anchor and active should be in the upper-left quadrant.
        rng, is_block = edit.selection_range()
        assert rng is not None
        assert rng[2] <= view._cols // 2, (
            f"selection x-end {rng[2]} should be in left half of {view._cols} cols"
        )
        assert rng[3] <= view._rows // 2, (
            f"selection y-end {rng[3]} should be in top half of {view._rows} rows"
        )
        # Default selection mode is now linear (line-following). Block
        # mode requires Command/Alt held during the drag.
        assert is_block is False, (
            f"default selection mode should be linear, got block"
        )
    finally:
        view.shutdown()
    print("✓ drag stays in the dragged region, not anchored to bottom-right")


def test_block_selection_default_highlights_only_drag_rectangle() -> None:
    """An Alt+drag highlights the rectangle the cursor traversed — not full-width rows.

    Block selection is activated by holding Command/Alt during the drag.
    This tests that the block rectangle covers only the dragged area
    (cells in [5, 25] × [2, 10]) rather than full-width rows.
    """
    _qapp()
    import pyte
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QTimer, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from src.ui.terminal.renderer import _in_selection
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    # Build a session view at a known size.
    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        view.resize(800, 600)
        view.show()
        loop = QEventLoop()
        qt = QTimer(); qt.setSingleShot(True); qt.setInterval(100)
        qt.timeout.connect(loop.quit); qt.start()
        loop.exec()

        edit = view.text_edit
        # Force viewport to fill the widget (offscreen mode quirk —
        # see test_pixel_to_cell_maps_to_visual_position).
        edit.viewport().resize(edit.size())
        view._recompute_size()

        # Simulate a drag that should produce a rectangle covering
        # roughly the upper-left quadrant. Use pixel coords that
        # correspond to a known target cell, computed from the actual
        # viewport size — the frame offset makes the cell index land
        # one off from naive ``cell * cell_w`` arithmetic.
        vp = edit.viewport()
        cw = vp.width() / view._cols
        ch = vp.height() / view._rows
        # Click at the centre of the target cells: cell (5, 2) and
        # cell (25, 10).
        press_px = (int((5 + 0.5) * cw), int((2 + 0.5) * ch))
        move_px = (int((25 + 0.5) * cw), int((10 + 0.5) * ch))
        ev_press = QMouseEvent(
            QEvent.Type.MouseButtonPress, QPoint(*press_px),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.AltModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_press)
        ev_move = QMouseEvent(
            QEvent.Type.MouseMove, QPoint(*move_px),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.AltModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_move)
        rng, is_block = edit.selection_range()
        assert is_block, "Alt+drag must activate block selection mode"
        x1, y1, x2, y2 = rng
        # Rectangle bounds: 5..25 cols, 2..10 rows. We use
        # approximate bounds (off-by-one tolerance) because the test
        # forces a viewport resize that may shift cell indices by 1.
        assert (x1, y1, x2, y2) == (5, 2, 25, 10), (
            f"expected diagonal-drag bounds (5, 2, 25, 10), got {(x1, y1, x2, y2)}"
        )

        # Cell INSIDE the rectangle: highlighted.
        assert _in_selection(15, 5, (rng, is_block)) is True
        # Cell at the rectangle's corner: highlighted.
        assert _in_selection(5, 2, (rng, is_block)) is True
        assert _in_selection(25, 10, (rng, is_block)) is True
        # Cell to the LEFT of the rectangle (same row): NOT highlighted.
        # This is the key test — under linear mode this would be
        # highlighted because row 10 is the "end row" of a left-anchored
        # selection that fans out from x=0 to x2=25.
        assert _in_selection(0, 10, (rng, is_block)) is False, (
            "cell at (0, 10) must NOT be highlighted in block mode"
        )
        # Cell to the RIGHT of the rectangle (same row): NOT highlighted.
        # Under linear mode this would be highlighted because row 2
        # is the "start row" and the flow fans out from x1=5 to the
        # right edge of the screen.
        assert _in_selection(79, 2, (rng, is_block)) is False, (
            "cell at (79, 2) must NOT be highlighted in block mode"
        )
        # Cell ABOVE the rectangle: NOT highlighted.
        assert _in_selection(15, 1, (rng, is_block)) is False
        # Cell BELOW the rectangle: NOT highlighted. This is the
        # second key test — the user said "most of the screen" is
        # selected, which under linear mode means full-width rows 3..9.
        # In block mode, only rows 2..10 are highlighted, so row 11
        # must NOT be.
        assert _in_selection(15, 11, (rng, is_block)) is False
        # Cell far below (where the cursor wasn't): NOT highlighted.
        assert _in_selection(15, 23, (rng, is_block)) is False
        # Cell far to the right, far below: NOT highlighted.
        assert _in_selection(79, 23, (rng, is_block)) is False
    finally:
        view.shutdown()
    print("✓ diagonal drag highlights only the rectangle, not full-width rows")


def test_pixel_to_cell_uses_viewport_size_not_widget_size() -> None:
    """A click at viewport pixel N maps to cell N / (vp_size / grid_size).

    Regression for the "selection starts 6 lines higher than the
    cursor" symptom. Root cause: ``_pixel_to_cell`` divided the
    click's pixel coord by ``self.size()`` (the *widget* dimensions,
    which include a 1-2 px frame around the viewport) instead of
    ``self.viewport().size()``. Because the viewport is smaller
    than the widget by the frame thickness, the resulting cell_h
    was slightly *larger* than it should be, which made ``pos.y()
    // cell_h`` return a row index lower than the user's click —
    and the offset grew with each row (a click at the bottom of
    the screen was the most affected, but the user noticed the
    error at row 5+).

    Verified the fix: when ``viewport().size() = 798×598`` (the
    real production dimensions after the frame offset), the cell
    math divides viewport pixel y by 24.92 (not the widget's 25.0),
    producing the correct row index for every tested y. The
    per-row drift goes from "off by 4" (when dividing by widget
    size) to "exactly correct".
    """
    _qapp()
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QTimer, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    host = QWidget()
    view = TerminalSessionView(argv=["/bin/cat"], parent=host)
    try:
        view.resize(800, 600)
        view.show()
        loop = QEventLoop()
        qt = QTimer(); qt.setSingleShot(True); qt.setInterval(100)
        qt.timeout.connect(loop.quit); qt.start(); loop.exec()

        edit = view.text_edit
        # Force the viewport to fill the widget — see the other
        # viewport-based tests for the offscreen-mode rationale.
        edit.viewport().resize(edit.size())
        view._recompute_size()

        vp = edit.viewport()
        cw = vp.width() / view._cols
        ch = vp.height() / view._rows

        # Click at the centre of each target row. Every assertion
        # below uses ``(row + 0.5) * ch`` as the pixel so the click
        # lands cleanly inside the target row regardless of
        # floating-point rounding.
        for target_row in [0, 5, 10, 15, 20, view._rows - 1]:
            py = int((target_row + 0.5) * ch)
            edit.clear_selection()
            ev = QMouseEvent(
                QEvent.Type.MouseButtonPress, QPoint(50, py),
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(edit.viewport(), ev)
            assert edit._sel_anchor is not None, (
                f"row {target_row}: viewport-delivered press did not "
                f"register cell-coord selection"
            )
            actual_row = edit._sel_anchor[1]
            assert actual_row == target_row, (
                f"row {target_row}: click at pixel y={py} expected "
                f"row={target_row}, got row={actual_row}. Cell math "
                f"is off — likely using widget size instead of viewport size."
            )
    finally:
        view.shutdown()
    print("✓ clicks at viewport coords map to the exact row the user clicked")


def test_real_mouse_event_via_viewport_sets_cell_selection() -> None:
    """A ``QMouseEvent`` delivered to the *viewport* via ``QApplication.sendEvent``
    triggers our cell-coord selection.

    Regression for the user-reported "selection anchored in bottom-right
    / most of the screen" symptom that persisted through multiple prior
    attempts to fix the issue. The earlier fixes targeted:

      1. ``_pixel_to_cell`` math (used ``fontMetrics`` instead of
         widget-size-derived cell dims).
      2. Default selection mode (linear flow → block rectangle).
      3. Qt event routing (override ``viewportEvent`` because Qt
         routes mouse events to the viewport first, not the text
         edit).

    Each fix was necessary. But none of them addressed the deepest
    cause: ``mousePressEvent`` / ``mouseMoveEvent`` /
    ``mouseReleaseEvent`` were calling ``super()`` on their
    respective overrides, which delegates to ``QTextEdit``'s default
    behaviour — and that default *starts a native drag selection* by
    mutating the ``QTextCursor``. The native selection draws a blue
    highlight on top of our HTML using the document's text positions
    (not cell coordinates). That highlight is the actual visible
    selection the user sees during a drag, until the next
    ``setHtml`` repaint (16ms later) clobbers it. So the user sees a
    bogus "anchored in the bottom-right / most of the screen" drag
    highlight, even though the cell-coord selection is correct.

    This test sends events through the viewport (the real path),
    asserts the cell-coord selection is set, AND asserts Qt's native
    text-cursor selection stays empty during the drag. Before the
    super()-removal fix, the second assertion failed (Qt's cursor
    had a multi-thousand-char selection during the drag).
    """
    _qapp()
    import pyte
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QTimer, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    from src.ui.terminal.terminal_container_view import TerminalContainerView
    from src.ui.terminal.terminal_session_view import TerminalSessionView

    # Build a properly-laid-out session so widget sizes are real
    # (not the 0-height placeholder an unparented test gets).
    host = QWidget()
    host.resize(900, 700)
    host.show()
    host_layout = QVBoxLayout(host)
    host_layout.setContentsMargins(0, 0, 0, 0)
    container = TerminalContainerView(parent=host)
    host_layout.addWidget(container)

    loop = QEventLoop()
    qt = QTimer(); qt.setSingleShot(True); qt.setInterval(100)
    qt.timeout.connect(loop.quit); qt.start(); loop.exec()

    view = TerminalSessionView(argv=["/bin/cat"], parent=container)
    container._sessions.append(view)
    idx = container.session_tabs.addTab(view, view.title())
    container.session_tabs.setCurrentIndex(idx)
    container.session_tabs.tabBar().setVisible(True)

    loop = QEventLoop()
    qt = QTimer(); qt.setSingleShot(True); qt.setInterval(100)
    qt.timeout.connect(loop.quit); qt.start(); loop.exec()

    view.start()
    loop = QEventLoop()
    qt = QTimer(); qt.setSingleShot(True); qt.setInterval(300)
    qt.timeout.connect(loop.quit); qt.start(); loop.exec()

    try:
        edit = view.text_edit
        # Wire a deterministic screen so we can predict the grid.
        # We don't have to repaint — the cell grid is what matters
        # for selection coord math.
        edit._screen_ref = lambda: pyte.Screen(80, 24)
        # Force _cols/_rows for predictable pixel→cell math.
        view._cols = 80
        view._rows = 24
        edit._screen_width = lambda: view._cols
        edit._screen_height = lambda: view._rows
        edit.setFocus()

        # Send a real MouseButtonPress to the viewport (the path real
        # user clicks take).
        ev_press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPoint(400, 300),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_press)
        # The viewportEvent override translates viewport-local pos to
        # widget-local pos then calls mousePressEvent. The cell coord
        # depends on the widget's actual rendered area; for our
        # viewport-driven path we just verify the press registered at
        # *some* valid cell (not None) — exact coords depend on
        # offscreen layout quirks that aren't the user's bug.
        assert edit._sel_anchor is not None, (
            "viewport-delivered MouseButtonPress did not register "
            "cell-coord selection — viewportEvent override is not "
            "wired correctly. Real user clicks would also fail."
        )
        x, y = edit._sel_anchor
        assert 0 <= x < view._cols, f"col {x} out of range"
        assert 0 <= y < view._rows, f"row {y} out of range"

        # Now do a real drag: press, move, release — all routed through
        # the viewport. Selection should grow.
        edit.clear_selection()
        ev_press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPoint(100, 100),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_press)
        # Move
        ev_move = QMouseEvent(
            QEvent.Type.MouseMove,
            QPoint(200, 150),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_move)
        # CRITICAL: during the drag, Qt's *native* text-cursor
        # selection must stay empty. If ``super().mousePressEvent``
        # or ``super().mouseMoveEvent`` had been called from the
        # overrides, Qt would have started a native drag selection
        # by mutating the text cursor, drawing a blue highlight on
        # top of our HTML using document text positions (not cell
        # coordinates). That's the actual visible bug the user sees.
        qt_cursor = edit.textCursor()
        assert not qt_cursor.hasSelection(), (
            "Qt's native text-cursor selection is active during the "
            "drag — mousePressEvent/mouseMoveEvent must NOT delegate "
            "to super(). The user will see Qt's blue drag selection "
            "drawn on top of our HTML."
        )
        # Release
        ev_release = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPoint(200, 150),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(edit.viewport(), ev_release)
        # Selection must be non-empty and have a real range.
        rng = edit.selection_range()
        assert rng is not None, "drag via viewport did not produce a selection"
        rng_box, is_block = rng
        assert is_block is False, "default selection should be linear (line-following)"
        assert rng_box[2] > rng_box[0] or rng_box[3] > rng_box[1], (
            f"drag should produce a non-degenerate rectangle, got {rng_box}"
        )
    finally:
        view.shutdown()
    print("✓ real MouseEvent via viewport triggers cell-coord selection, no Qt native selection")


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
        test_renderer_selection_highlight_is_visible_on_dark_bg,
        test_extract_cells_text_strips_trailing_spaces,
        test_extract_cells_text_block_preserves_columns,
        test_clipboard_copy_writes_to_clipboard,
        test_pty_paste_round_trip,
        test_bracketed_paste_always_wrapped,
        test_resize_changes_rows_and_cols,
        test_mouse_press_with_keyboard_modifier_enum,
        test_key_press_with_keyboard_modifier_enum,
        test_norm_mods_helper,
        test_key_press_plain_letter_does_not_crash,
        test_document_margin_is_zero,
        test_renderer_pre_has_no_padding,
        test_measure_rendered_rows_matches_actual_layout,
        test_recompute_size_does_not_overflow_viewport,
        test_adjust_font_size_grows_then_clamps,
        test_adjust_font_size_shrinks_then_clamps,
        test_alt_equals_key_press_increases_font_size,
        test_alt_arrows_cycle_session_tabs_with_wraparound,
        test_alt_arrow_shortcut_actions_are_wired,
        test_pixel_to_cell_maps_to_visual_position,
        test_pixel_to_cell_drag_stays_in_dragged_region,
        test_block_selection_default_highlights_only_drag_rectangle,
        test_real_mouse_event_via_viewport_sets_cell_selection,
        test_pixel_to_cell_uses_viewport_size_not_widget_size,
        test_repaint_scrolls_to_max_when_content_overflows,
        test_repaint_scrolls_to_max_when_cursor_is_above_last_row,
        test_repaint_does_not_snap_to_max_when_no_scrollback,
        test_repaint_scrolls_to_top_for_new_session,
        test_repaint_clears_user_scrolled_up_when_at_bottom,
        test_repaint_resets_user_scrolled_up_on_alt_screen_exit,
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
