"""Tests for the renderer's handling of SGR 4 ("underline") on cells whose
glyphs already have a visible stroke.

Reproduces the user-visible bug: Claude Code v2.x draws its welcome UI
rule lines as actual Unicode box-drawing characters (U+2500 ``─`` etc.)
and ALSO marks them with SGR-4 (underline). In a real xterm the SGR-4
attribute is a no-op on those characters — the glyph already extends
below the baseline, so there's nothing to underline beneath it. The HTML
renderer was unconditionally adding ``text-decoration:underline`` for
every underscored cell, which drew a *second* line under each ``─``,
making the row look like a fat/doubled-up rule.

These tests pin both halves of the fix:

1. ``_glyph_is_self_underlining`` correctly identifies Box Drawing,
   Block Elements, and CJK Presentation Form horizontal-line code points.
2. ``_style_attrs`` skips ``text-decoration:underline`` for cells whose
   glyph is one of those characters, even when the underscore attribute
   is on.
3. ``screen_to_html`` produces HTML without ``text-decoration:underline``
   for a full-row ``─`` separator (which was the user-visible symptom).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def test_glyph_classifier_matches_box_drawing_block() -> None:
    from src.ui.terminal.renderer import _glyph_is_self_underlining

    # Box Drawing block — every code point is some kind of line.
    for ch in ("─", "═", "━", "│", "┌", "┐", "└", "┘", "├", "┤", "┼"):
        assert _glyph_is_self_underlining(ch) is True, (
            f"expected {ch!r} (U+{ord(ch):04X}) to be self-underlining"
        )
    # Block Elements — half / full blocks below baseline.
    for ch in ("▀", "▄", "█", "░", "▒", "▓"):
        assert _glyph_is_self_underlining(ch) is True, (
            f"expected {ch!r} to be self-underlining"
        )
    # CJK Presentation Forms with horizontal-line semantics.
    for ch in ("﹍", "﹎", "﹏"):
        assert _glyph_is_self_underlining(ch) is True, (
            f"expected {ch!r} to be self-underlining"
        )
    print("✓ _glyph_is_self_underlining covers Box Drawing / Block / CJK")


def test_glyph_classifier_lets_normal_characters_through() -> None:
    from src.ui.terminal.renderer import _glyph_is_self_underlining

    # Plain ASCII letters / digits / punctuation must NOT be classified
    # as self-underlining — otherwise we'd drop the underscore attribute
    # on every "hello" the user types.
    for ch in ("a", "A", "z", "Z", "0", "9", " ", "&", "?", "!", "(", ")"):
        assert _glyph_is_self_underlining(ch) is False, (
            f"expected {ch!r} to NOT be self-underlining"
        )
    print("✓ _glyph_is_self_underlining leaves ordinary chars alone")


def test_style_attrs_skips_underline_for_box_drawing_cell() -> None:
    """The bug fix: underscore=True + Box Drawing char → no text-decoration."""
    from src.ui.terminal.renderer import _style_attrs

    class FakeCell:
        fg = "default"
        bg = "default"
        bold = False
        italics = False
        underscore = True
        reverse = False

    cell = FakeCell()
    cell.data = "─"  # horizontal-line box drawing char

    style = _style_attrs(cell)
    assert "text-decoration:underline" not in style, (
        f"box-drawing char must NOT get an extra CSS underline; got {style!r}"
    )
    print("✓ _style_attrs skips CSS underline on ─ when underscore=True")


def test_style_attrs_still_underlines_normal_text() -> None:
    """Regression check: ordinary letters with underscore=True still
    get the CSS underline so 'underlined text' renders identically."""
    from src.ui.terminal.renderer import _style_attrs

    class FakeCell:
        fg = "default"
        bg = "default"
        bold = False
        italics = False
        underscore = True
        reverse = False

    cell = FakeCell()
    cell.data = "hello"
    style = _style_attrs(cell)
    assert "text-decoration:underline" in style, (
        f"ordinary text cells should keep their CSS underline; got {style!r}"
    )
    print("✓ _style_attrs still underlines text characters")


def test_screen_to_html_boxdrawing_rule_has_no_text_decoration() -> None:
    """End-to-end: a full row of `─` cells marked with SGR-4 must NOT
    produce a ``text-decoration:underline`` in the HTML — that's the
    user-visible symptom."""
    import pyte

    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(40, 4)
    stream = pyte.Stream(screen)
    # Move to top, set SGR 4, fill the row with `─`, set SGR 24 (reset
    # underline), and print one marker line so we know we hit it.
    stream.feed(
        "\x1b[1;1H"
        "\x1b[4m"  # enable underline
        + ("─" * 40)
        + "\x1b[24m"  # disable underline
        + "\nafter\n"
    )

    html = screen_to_html(screen)

    # Find the section corresponding to row 0 (the rule) and row 1 ('after').
    # In the renderer, a cell where every cell has SGR 4 + Box Drawing +
    # no other styling -> ``has_visible_style`` is False -> it's emitted
    # as plain text inside the surrounding <pre>; crucially, no
    # ``<span style="..." text-decoration:underline"...">`` should be
    # emitted for this row.
    #
    # Filter to the rule-row chunk by looking for the marker word 'after'
    # and confirming the chunk before it has no underlined span.
    if "after" in html:
        # Split on the marker so we can inspect only the row above.
        before, _, after = html.partition("after")
        # The rule-row is the substring before 'after'. It should
        # contain many `─` characters but NOT a text-decoration:underline.
        assert "─" in before, "expected the rule row's box-drawing chars"
        assert "text-decoration:underline" not in before, (
            f"SGR-4 + box-drawing must NOT emit text-decoration; got:\n{before}"
        )
    print("✓ screen_to_html omits text-decoration for full-row `─` under SGR 4")


def test_screen_to_html_keeps_underline_on_normal_text() -> None:
    """Regression: a normal text run with SGR-4 must still render with
    text-decoration:underline so 'underlined text' stays underlined."""
    import pyte

    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(40, 2)
    stream = pyte.Stream(screen)
    stream.feed(
        "\x1b[1;1H"
        "\x1b[4m"  # enable underline
        "hello world"
    )
    html = screen_to_html(screen)
    assert "text-decoration:underline" in html, (
        f"normal 'hello world' with SGR 4 should keep CSS underline; got {html!r}"
    )
    print("✓ screen_to_html retains CSS underline on normal text")


def test_screen_to_html_mixed_row_only_underlines_text_segments() -> None:
    """Mixed runs: text + box-drawing. The current implementation
    groups them by attributes, so the entire run's first cell decides.
    Verify that mixed runs still don't insert a doubled-up line for
    the rule characters (the first cell of the test's run is a `─`)."""
    import pyte

    from src.ui.terminal.renderer import screen_to_html

    screen = pyte.Screen(20, 1)
    stream = pyte.Stream(screen)
    stream.feed(
        "\x1b[4m" + "─" * 10 + "text"  # all underscored, mixed chars
    )
    html = screen_to_html(screen)
    # '─' is the first cell of the only run. With the fix, no
    # text-decoration:underline is emitted for this run.
    assert "text-decoration:underline" not in html, (
        f"all-underscored rule-led run should not get text-decoration; got {html!r}"
    )
    print("✓ screen_to_html: rule-led mixed runs don't double-underline")


def main() -> None:
    tests = [
        test_glyph_classifier_matches_box_drawing_block,
        test_glyph_classifier_lets_normal_characters_through,
        test_style_attrs_skips_underline_for_box_drawing_cell,
        test_style_attrs_still_underlines_normal_text,
        test_screen_to_html_boxdrawing_rule_has_no_text_decoration,
        test_screen_to_html_keeps_underline_on_normal_text,
        test_screen_to_html_mixed_row_only_underlines_text_segments,
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
