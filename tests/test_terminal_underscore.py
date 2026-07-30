"""Tests that the renderer does NOT emit ``text-decoration:underline``.

The user's experience: Claude Code v2.x leaves the SGR-4 (underline)
attribute on after drawing its welcome UI, so every cell the user types
or the assistant writes inherits ``underscore=True`` on the pyte side.
A real xterm would render those as underlined; in QTextDocument the
CSS underline is *much* heavier visually, and the entire screen ends
up looking underlined. The user asked for that to stop.

The renderer intentionally drops SGR-4 on the emit side. The
underscore attribute still flows through the grouping key (so the
run-grouping logic is consistent), but the ``<span>`` style never
includes ``text-decoration:underline``. This is a feature trade-off:
we lose the ability to render legitimate terminal-emulator underlines
(e.g. underlined links in ``man`` pages) for a clean visual result;
a future toggle could re-enable it if needed.

What this test file pins:

1. The HTML never contains ``text-decoration:underline`` regardless of
   the cell's underlying SGR-4 state.
2. Cells whose only attribute is ``underscore=True`` (no fg/bg/bold/…)
   render as plain text — exactly like any other default-styled cell.
3. Cells with a non-default fg/bg still render with that styling
   (i.e. we don't blow away the rest of the styling along with the
   underline).
4. Real terminal content (boxes, real text, mixed runs) renders cleanly
   with no phantom underlines introduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


# ---- helpers ----------------------------------------------------------------


class _FakeCell:
    """Minimal pyte-like cell carrying the fields the renderer reads."""

    def __init__(
        self,
        data: str = " ",
        *,
        fg: str = "default",
        bg: str = "default",
        bold: bool = False,
        italics: bool = False,
        underscore: bool = False,
        reverse: bool = False,
    ) -> None:
        self.data = data
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.italics = italics
        self.underscore = underscore
        self.reverse = reverse


def _has_text_decoration_underline(html: str) -> bool:
    return "text-decoration:underline" in html


# ---- direct style-emit checks ----------------------------------------------


def test_style_attrs_never_emits_underline_for_underscore_only_cell() -> None:
    """Cell with all-default attributes except underscore=True →
    the resulting inline style must NOT contain text-decoration."""
    from src.ui.terminal.renderer import _style_attrs

    cell = _FakeCell("hello", underscore=True)
    style = _style_attrs(cell)
    assert "text-decoration:underline" not in style, (
        f"underscore must not produce CSS underline; got {style!r}"
    )
    print("✓ _style_attrs drops text-decoration for plain text with SGR-4")


def test_style_attrs_never_emits_underline_for_box_drawing_cell() -> None:
    """Box-drawing with SGR-4 — same expectation as plain text."""
    from src.ui.terminal.renderer import _style_attrs

    cell = _FakeCell("─", underscore=True)
    style = _style_attrs(cell)
    assert "text-decoration:underline" not in style
    print("✓ _style_attrs drops text-decoration for box-drawing + SGR-4")


def test_style_attrs_keeps_other_styling_alongside_no_underline() -> None:
    """When a cell has a non-default fg/bg AND SGR-4, we keep the
    fg/bg styling but drop the underline."""
    from src.ui.terminal.renderer import _style_attrs

    cell = _FakeCell("hi", fg="red", bg="default", underscore=True)
    style = _style_attrs(cell)
    assert "text-decoration:underline" not in style
    # The "red" pyte colour maps to xterm's #cc0000 hex; the rendered
    # ``color:`` is what we care about.
    assert "color:#cc0000" in style, f"fg color must survive; got {style!r}"
    print("✓ _style_attrs keeps fg/bg styling, drops underline")


# ---- end-to-end HTML checks --------------------------------------------------


def test_screen_to_html_omits_text_decoration_for_full_underscored_rule() -> None:
    """A row of `─` cells with SGR-4 → no text-decoration in the HTML."""
    import pyte

    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(80, 5)
    stream = pyte.Stream(screen)
    stream.feed(
        "\x1b[1;1H"
        "\x1b[4m"
        + ("─" * 40)
        + "\x1b[24m"
        + "\nafter\n"
    )
    html = screen_to_html(screen)
    before_marker, _, _ = html.partition("after")
    assert "─" in before_marker
    assert not _has_text_decoration_underline(before_marker), (
        f"underscored rule row leaked a CSS underline; got {before_marker!r}"
    )
    print("✓ screen_to_html: full row of `─` with SGR-4 → no text-decoration")


def test_screen_to_html_omits_text_decoration_for_underscored_text() -> None:
    """Plain text with SGR-4 — same expectation; the user explicitly
    asked for the underline to be suppressed."""
    import pyte

    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(40, 2)
    stream = pyte.Stream(screen)
    stream.feed("\x1b[1;1H" + "\x1b[4m" + "hello world")
    html = screen_to_html(screen)
    assert not _has_text_decoration_underline(html), (
        f"real terminal text should not be underlined; got {html!r}"
    )
    assert "hello" in html and "world" in html, "text content lost"
    print("✓ screen_to_html: plain text with SGR-4 → no text-decoration")


def test_screen_to_html_omits_text_decoration_for_real_claude_output() -> None:
    """End-to-end: run the actual Claude Code TUI, capture its
    pyte-screen state, render through our pipeline, and confirm zero
    text-decoration:underline makes it into the HTML."""
    import pty, os, time, select, signal

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(
            "claude", ["claude"], {**os.environ, "TERM": "xterm-256color"}
        )

    import pyte
    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(120, 60)
    stream = pyte.Stream(screen)
    deadline = time.time() + 4.0
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        stream.feed(chunk.decode("utf-8", errors="replace"))

    html = screen_to_html(screen)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    assert not _has_text_decoration_underline(html), (
        "real Claude Code TUI produced HTML with text-decoration:underline — "
        "the user's earlier complaint was about this; the carve-out has "
        "regressed. Inspect the raw HTML to debug."
    )
    print("✓ screen_to_html: real Claude Code output → zero text-decoration")


def test_screen_to_html_keeps_other_styling_with_underscore() -> None:
    """Regression: dropping the underline must not also drop fg/bg/etc.
    A cell with explicit fg color + SGR-4 must still render with the
    fg color (just no underline)."""
    import pyte

    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(20, 1)
    stream = pyte.Stream(screen)
    stream.feed("\x1b[1;1H\x1b[4m\x1b[31m" + "hello")  # SGR 4 + red fg
    html = screen_to_html(screen)
    assert not _has_text_decoration_underline(html)
    # The red color should still be there.
    assert "color:" in html and "hello" in html, (
        f"fg color or text content lost; got {html!r}"
    )
    print("✓ screen_to_html: fg styling survives without underline")


def main() -> None:
    tests = [
        test_style_attrs_never_emits_underline_for_underscore_only_cell,
        test_style_attrs_never_emits_underline_for_box_drawing_cell,
        test_style_attrs_keeps_other_styling_alongside_no_underline,
        test_screen_to_html_omits_text_decoration_for_full_underscored_rule,
        test_screen_to_html_omits_text_decoration_for_underscored_text,
        test_screen_to_html_omits_text_decoration_for_real_claude_output,
        test_screen_to_html_keeps_other_styling_with_underscore,
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
    print("\n✅ All terminal-underscore tests passed!")


if __name__ == "__main__":
    main()
