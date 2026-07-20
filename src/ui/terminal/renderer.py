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
    rows are full-width. This matches xterm's "drag flows across rows"
    behaviour.
    Block: simple rectangle test — same x-range, same y-range.
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
    if force_reverse and bg == "var(--term-bg)":
        parts.append("background-color:rgba(255,255,255,0.15)")
    if getattr(cell, "bold", False):
        parts.append("font-weight:bold")
    if getattr(cell, "italics", False):
        parts.append("font-style:italic")
    if getattr(cell, "underscore", False):
        parts.append("text-decoration:underline")
    return ";".join(parts)


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
        'margin:0;padding:4px;'
        'background-color:var(--term-bg);'
        'color:var(--term-fg);'
        'line-height:1.2;'
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
