"""Render a pyte ``Screen`` as HTML suitable for ``QTextEdit.setHtml``.

This module is **pure-Python and Qt-free** so it can be unit-tested without
a display server. The output is a single ``<pre>`` element with one or more
``<span>`` children grouped by identical styling — fewer nodes means faster
``setHtml`` calls.

Style model
-----------
Pyte exposes per-cell attributes ``fg``, ``bg``, ``bold``, ``italics``,
``underscore``, ``reverse`` (and a few more). We collapse those into a small
CSS attribute tuple and emit one ``<span>`` per run of consecutive cells with
identical attributes. The cursor cell is rendered with fg/bg swapped so it
stands out without needing a blinking caret.

Colour mapping
--------------
Pyte's ``fg`` / ``bg`` strings are one of:

- A named 16-colour (``"red"``, ``"bright_green"``, …).
- A 256-colour hex (``"af0000"`` — 6 lowercase hex digits).
- ``"default"`` — falls through to the surrounding terminal theme.

We render named colours against a fixed dark-theme palette (terminals default
to dark; users can later override via CSS), 256-colour as ``#rrggbb``, and
``"default"`` as the inherited CSS variable (``var(--term-fg)`` /
``var(--term-bg)``).

Public surface
--------------
``screen_to_html(screen, *, cursor=(x, y), show_cursor=True) -> str``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyte import Screen


# --- Colour palettes ---------------------------------------------------------
#
# A small dark-theme palette matching xterm's 16-colour defaults. The HTML
# output is wrapped in a <pre> with these as CSS variables so the host
# widget (QTextEdit) gets sensible defaults without extra styling.
#
_DARK_FG = "#d0d0d0"
_DARK_BG = "#1a1a1a"

_16_COLOR_HEX: dict[str, str] = {
    "black":          "#000000",
    "red":            "#cc0000",
    "green":          "#4e9a06",
    "brown":          "#c4a000",
    "blue":           "#3465a4",
    "magenta":        "#75507b",
    "cyan":           "#06989a",
    "white":          "#d3d7cf",
    "bright_black":   "#555753",
    "bright_red":     "#ef2929",
    "bright_green":   "#8ae234",
    "bright_yellow":  "#fce94f",
    "bright_blue":    "#729fcf",
    "bright_magenta": "#ad7fa8",
    "bright_cyan":    "#34e2e2",
    "bright_white":   "#eeeeec",
}


def _color_to_css(value: str, role: str) -> str:
    """Map a pyte colour string to a CSS colour value.

    ``role`` is ``"fg"`` or ``"bg"`` — used only to pick the right
    CSS-variable fallback for the ``"default"`` case.
    """
    if value == "default":
        return f"var(--term-{role})"
    if value in _16_COLOR_HEX:
        return _16_COLOR_HEX[value]
    # pyte 256-colour is six lowercase hex digits with no leading '#'.
    if len(value) == 6 and all(c in "0123456789abcdef" for c in value):
        return f"#{value}"
    # Unknown shape — fall back to the inherited colour rather than render
    # raw text that could break CSS parsing.
    return f"var(--term-{role})"


def _cell_style(cell) -> tuple[str, str]:
    """Compute the (fg, bg) CSS strings for a pyte Char cell.

    Honours the ``reverse`` attribute (swap fg/bg) since that's how
    terminals render highlighted regions and selected text.
    """
    if getattr(cell, "reverse", False):
        return _color_to_css(cell.bg, "fg"), _color_to_css(cell.fg, "bg")
    return _color_to_css(cell.fg, "fg"), _color_to_css(cell.bg, "bg")


def _attrs_key(cell, *, selected: bool = False) -> tuple:
    """Hashable tuple identifying a cell's styling for run-grouping.

    ``selected`` is part of the key so a span that runs across a
    selection boundary splits at the boundary — the selected half gets
    the reverse-rendered highlight without us touching the DOM after
    ``setHtml``.
    """
    return (
        cell.fg,
        cell.bg,
        bool(getattr(cell, "bold", False)),
        bool(getattr(cell, "italics", False)),
        bool(getattr(cell, "underscore", False)),
        bool(getattr(cell, "reverse", False)),
        bool(selected),
    )


def _in_selection(
    x: int,
    y: int,
    selection: "tuple[tuple[int, int, int, int], bool] | None",
) -> bool:
    """True if cell ``(x, y)`` is inside the selection rectangle.

    Linear: per-row x clamp — row y1 has x≥x1, row y2 has x≤x2, interior
    rows are full-width. Matches xterm's "drag flows across rows"
    behaviour. Currently unused by the terminal UI (block is the
    default) but kept so a future mode toggle can re-enable it without
    rewriting the highlight math.

    Block: simple rectangle test — same x-range, same y-range. This is
    the default selection mode for the terminal because on a uniform
    character grid every row contains meaningful content, so the
    "linear flow" interpretation produces confusing results where
    diagonal drags highlight most of the screen.
    """
    if selection is None:
        return False
    (x1, y1, x2, y2), is_block = selection
    if is_block:
        return x1 <= x <= x2 and y1 <= y <= y2
    if y < y1 or y > y2:
        return False
    if y == y1 and y == y2:
        return x1 <= x <= x2
    if y == y1:
        return x >= x1
    if y == y2:
        return x <= x2
    return True


def _is_cursor(cell_pos: tuple[int, int], cursor: tuple[int, int]) -> bool:
    return cell_pos == cursor


def _escape(text: str) -> str:
    """HTML-escape a cell's data, preserving a single space as ``&nbsp;``
    so trailing whitespace doesn't get collapsed by the browser."""
    out = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return out.replace(" ", "&nbsp;")


def _style_attrs(cell, *, force_reverse: bool = False) -> str:
    """Build the inline ``style="..."`` string for one styled span."""
    if force_reverse:
        fg, bg = _color_to_css(cell.bg, "fg"), _color_to_css(cell.fg, "bg")
    else:
        fg, bg = _cell_style(cell)

    parts = [f"color:{fg}", f"background-color:{bg}"]
    # Selection highlight on default-styled cells: both fg and bg are
    # "default" so force-reverse alone renders fg on bg with no visible
    # contrast. Add a distinctive background tint so the selected region
    # stands out against the surrounding text. Only do this when the
    # already-computed bg is the inherited (default) variable — colored
    # rows already produce visible reverse contrast.
    #
    # NB: we use a solid color, not ``rgba(..., 0.15)`` semi-transparent
    # white. CSS treats multiple ``background-color`` declarations as
    # overrides (the *last* one wins), not as a layered fill — so
    # adding ``background-color:rgba(255,255,255,0.15)`` REPLACES the
    # dark terminal bg with semi-transparent white, which against light
    # foreground text produces near-zero contrast (the highlight
    # effectively disappears). A solid slightly-brighter grey keeps
    # the original fg visible and gives the selection a clear
    # difference from the surrounding default-bg cells.
    if force_reverse and bg == "var(--term-bg)":
        # ``_DARK_BG`` is the terminal's default bg (#1a1a1a). We
        # lighten it to #3a3a3a for selection — about 20% brighter,
        # which gives clear contrast with both the unselected cells
        # and the (lighter) terminal foreground.
        parts.append("background-color:#3a3a3a")
    if getattr(cell, "bold", False):
        parts.append("font-weight:bold")
    if getattr(cell, "italics", False):
        parts.append("font-style:italic")
    if getattr(cell, "underscore", False) and not _glyph_is_self_underlining(
        cell.data
    ):
        # SGR-4 ("underline") on cells whose glyph already extends below
        # the baseline would render as a *second* horizontal line under
        # the existing character. Box-drawing characters (U+2500–U+259F,
        # which Claude Code v2.x uses for its welcome-screen borders) are
        # the main offender; see :func:`_glyph_is_self_underlining` for
        # the full set. In a real xterm the SGR-4 attribute is a no-op
        # on those characters, so skipping it here brings the renderer
        # in line with how a native terminal would draw them.
        parts.append("text-decoration:underline")
    return ";".join(parts)


def _glyph_is_self_underlining(ch: str) -> bool:
    """True when ``ch``'s glyph already has a visible horizontal stroke
    below the baseline.

    Rendering such a glyph with CSS ``text-decoration:underline`` would
    draw a *second* line under the existing character — visually the
    cell becomes a "doubled-up" / over-thick horizontal rule compared
    to what a real xterm would show. The main offenders are Claude
    Code v2.x's box-drawing separator lines (``─``, ``═``, …) which
    it emits with the SGR-4 underline attribute set; without this
    filter the renderer paints a phantom underline beneath every one
    of those glyphs.

    Matched ranges:

    - ``U+2500–U+257F``: Box Drawing block — every code point is some
      kind of line / corner.
    - ``U+2580–U+259F``: Block Elements — half- and full-block cells
      sit on/below the baseline; a CSS underline would double-stroke
      them too.
    - ``U+FE4D–U+FE4F``: CJK Compatibility horizontal lines
      (PRESENTATION FORM FOR VERTICAL / HORIZONTAL ELLIPSIS / STROKE).
    """
    if not ch:
        return False
    cp = ord(ch[0])
    return (
        0x2500 <= cp <= 0x257F
        or 0x2580 <= cp <= 0x259F
        or 0xFE4D <= cp <= 0xFE4F
    )


def screen_to_html(
    screen: "Screen",
    *,
    cursor: tuple[int, int] | None = None,
    show_cursor: bool = True,
    selection: "tuple[tuple[int, int, int, int], bool] | None" = None,
) -> str:
    """Render a pyte ``Screen`` as an HTML string.

    Parameters
    ----------
    screen
        The pyte ``Screen`` instance (or anything with a ``.buffer[y][x]``
        2-D array of Char-like cells).
    cursor
        ``(x, y)`` cursor position. If ``None``, the screen's own
        ``.cursor`` is used.
    show_cursor
        If ``True`` the cell under the cursor is rendered with reversed
        colours so it's visually distinguishable.
    selection
        Optional ``((x1, y1, x2, y2), is_block)`` tuple. Cells inside the
        selection rectangle are rendered with fg/bg swapped (the xterm
        highlight). For linear selections the rectangle flows row-to-row;
        for block selections it's a fixed column range across all rows.
        ``None`` (the default) means no highlight — backward-compatible
        with callers that don't pass it.
    """
    if cursor is None:
        try:
            cursor = (int(screen.cursor.x), int(screen.cursor.y))
        except Exception:  # noqa: BLE001
            cursor = (0, 0)

    rows = screen.lines
    cols = screen.columns

    out: list[str] = [
        '<pre style="'
        'font-family:Menlo,Consolas,monospace;'
        f'--term-fg:{_DARK_FG};'
        f'--term-bg:{_DARK_BG};'
        # Padding must be 0 — QTextDocument already adds its own
        # document-margin on top of anything we put here, so any
        # extra pixels compound into rows of vertical slack. The
        # cell grid is exactly the viewport's pixel area.
        'margin:0;padding:0;'
        'background-color:var(--term-bg);'
        'color:var(--term-fg);'
        # Line-height:1.0 makes ``font.lineSpacing()`` (which renders
        # the row baseline measurement) match the actual rendered row
        # height — otherwise QTextDocument lays out slightly taller
        # rows than ``_recompute_size`` measures, and the document
        # ends up ``rows + 2`` tall, leaving room for vertical scroll.
        'line-height:1.0;'
        '">'
    ]

    for y in range(rows):
        # Walk the row, grouping consecutive cells with identical attributes
        # (and cursor-state AND selection-membership) into a single <span>.
        x = 0
        while x < cols:
            cell = screen.buffer[y][x]
            here = (x, y)
            is_cursor_here = show_cursor and _is_cursor(here, cursor)
            is_selected_here = _in_selection(x, y, selection)

            # Collect the run length — cells that share attributes,
            # cursor-state, AND selection-membership with the first cell.
            run_attrs = _attrs_key(cell, selected=is_selected_here)
            run_cursor = is_cursor_here
            run_selected = is_selected_here
            end = x + 1
            while end < cols:
                nxt = screen.buffer[y][end]
                nxt_cursor = show_cursor and _is_cursor((end, y), cursor)
                nxt_selected = _in_selection(end, y, selection)
                if _attrs_key(nxt, selected=nxt_selected) != run_attrs:
                    break
                if nxt_cursor != run_cursor:
                    break
                if nxt_selected != run_selected:
                    break
                end += 1

            run_text = "".join(screen.buffer[y][i].data for i in range(x, end))

            # A run is "visible" if it carries non-default styling OR
            # contains a cursor cell OR contains a selected cell —
            # otherwise plain text in the surrounding <pre> already
            # renders correctly with no span.
            has_visible_style = (
                run_attrs[0] != "default"
                or run_attrs[1] != "default"
                or run_attrs[2]
                or run_attrs[3]
                or run_attrs[4]
                or run_attrs[5]
                or run_cursor
                or run_selected
            )

            if has_visible_style:
                # Selection highlight renders as fg/bg-swap (xterm
                # convention). Cursor also force-reverses; when both
                # apply on the same cell the cursor swap wins — it's
                # the same render path, no double-swap.
                force_reverse = run_cursor or run_selected
                out.append(
                    f'<span style="{_style_attrs(cell, force_reverse=force_reverse)}">'
                    f"{_escape(run_text)}"
                    "</span>"
                )
            else:
                out.append(_escape(run_text))

            x = end

        # End of row — newline (except after the last row, to avoid a
        # trailing blank line).
        if y < rows - 1:
            out.append("\n")

    out.append("</pre>")
    return "".join(out)
