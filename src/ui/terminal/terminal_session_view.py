"""One terminal session, embedded in a Qt widget.

Composition:

* A ``PtySession`` (QThread) owns the PTY and reader loop.
* A ``pyte.Screen`` + ``pyte.ByteStream`` keep the virtual terminal state.
* A subclassed ``QTextEdit`` renders the screen as HTML and forwards every
  keypress to the PTY.

Per-keystroke forwarding
------------------------
We deliberately do **not** buffer input in Python. The kernel PTY line
discipline in canonical mode already handles echo + line buffering; if we
also buffered, raw-mode apps like ``vim`` / ``less`` and ``Ctrl+C`` would
break. So ``keyPressEvent`` emits individual bytes (``b"a"``, ``b"\x03"``,
``b"\x1b[A"``) directly to the PTY.

Idempotent ``shutdown()``
-------------------------
Mirrors ``RecentActivityView.shutdown()`` (see
``src/ui/recent_activity_view.py:171``): safe to call multiple times, safe
to call before ``start()``, safe to call from the GUI thread.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QKeyEvent,
    QMouseEvent,
    QTextCursor,
)
from PySide6.QtWidgets import QTextEdit, QVBoxLayout, QWidget

from src.ui.terminal.renderer import history_lines_to_html, screen_to_html


# --- Pure keymap (testable without Qt) -------------------------------------

def _norm_mods(mods) -> int:
    """Coerce a Qt ``KeyboardModifiers`` (or plain int, or ``None``) to ``int``.

    ``event.modifiers()`` returns a ``Qt.KeyboardModifiers`` flag enum.
    ``int(flag_enum)`` is overloaded on most PySide6 builds but raises
    ``TypeError: int() argument must be ... not 'KeyboardModifier'`` on
    others (this dev environment). Calling ``int(mods.value)`` works
    on every build that exposes ``.value`` (i.e., every supported
    PySide6 release).

    Used uniformly by every modifier-bit test in this file — without
    it, a real keystroke carrying modifier flags blows up
    ``keyPressEvent`` / ``mousePressEvent`` and the override silently
    swallows the press, breaking typing / selection. Centralising the
    coercion here means there's exactly one safe idiom to learn.
    """
    if mods is None:
        return 0
    if isinstance(mods, int):
        return mods
    return int(mods.value)


# A tiny dataclass-free namespace so this can be imported in renderer-only
# tests without pulling in PySide6.
def key_to_bytes(
    key: int,
    *,
    modifiers: int = 0,
    text: str = "",
) -> bytes | None:
    """Translate a Qt key event into bytes for the PTY.

    Returns ``None`` if the key should be ignored (e.g. modifier-only).

    The mapping mirrors xterm defaults — arrow keys as CSI sequences, Ctrl
    letters as the corresponding control byte, etc.

    ``modifiers`` may be a plain ``int`` (from ``event.modifiers()``) or a
    ``Qt.KeyboardModifier`` value — both work because we coerce to ``int``
    via ``_norm_mods`` before the bitmask comparisons.
    """
    mod_mask = _norm_mods(modifiers)
    ctrl_held = bool(mod_mask & int(Qt.ControlModifier.value))
    alt_held = bool(mod_mask & int(Qt.AltModifier.value))
    meta_held = bool(mod_mask & int(Qt.MetaModifier.value))

    # Plain printable text always wins for unmodified key presses (lets
    # `text` (which honours keyboard layout) drive the output rather than
    # ``key`` (which doesn't know about layout-specific characters)).
    if text and not (ctrl_held or alt_held or meta_held):
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            return b"\t"
        return text.encode("utf-8")

    # Special keys.
    if key in (Qt.Key_Return, Qt.Key_Enter):
        return b"\r"
    if key == Qt.Key_Backspace:
        return b"\x7f"
    if key == Qt.Key_Tab:
        return b"\t"
    if key == Qt.Key_Escape:
        return b"\x1b"
    if key == Qt.Key_Up:
        return b"\x1b[A"
    if key == Qt.Key_Down:
        return b"\x1b[B"
    if key == Qt.Key_Right:
        return b"\x1b[C"
    if key == Qt.Key_Left:
        return b"\x1b[D"
    if key == Qt.Key_Home:
        return b"\x1b[H"
    if key == Qt.Key_End:
        return b"\x1b[F"
    if key == Qt.Key_Delete:
        return b"\x1b[3~"
    if key == Qt.Key_PageUp:
        return b"\x1b[5~"
    if key == Qt.Key_PageDown:
        return b"\x1b[6~"
    if key == Qt.Key_Insert:
        return b"\x1b[2~"

    # Ctrl + letter → control byte.
    if ctrl_held and Qt.Key_A <= key <= Qt.Key_Z:
        return bytes([key - Qt.Key_A + 1])

    # Fall through to the text if we have it (e.g. shifted punctuation).
    if text:
        return text.encode("utf-8")
    return None


# --- Widget ----------------------------------------------------------------


class TerminalTextEdit(QTextEdit):
    """QTextEdit subclass that forwards all keypresses to a callback.

    Selection model
    ---------------
    Selection lives in cell coordinates (col, row) in our own state —
    not Qt's native ``QTextCursor`` selection. The two reasons:

    1. ``_repaint()`` calls ``setHtml`` every ~16 ms, which destroys any
       native selection the user makes.
    2. The original ``mousePressEvent`` already swallowed the press
       without delegating to ``super()`` so Qt's drag selection was
       never starting anyway.

    By keeping selection state here and threading it through the
    renderer on every repaint, the highlight survives the document
    rebuilds for free — and ``_copy_selection`` walks the pyte buffer
    directly, returning the exact bytes the shell would echo.

    Modes
    -----
    * **Linear** (default): drag flows across rows like a text editor —
      selection follows text flow, wrapping to the next line at the end
      of each row. Start row covers x1..end, end row covers 0..x2,
      intermediate rows are full-width.
    * **Block** (Command/Alt held): the selection is the rectangle the
      cursor traversed — the same column window across every touched row.
      Useful for selecting columnar data (e.g. ``ls -l`` output). On
      copy, column padding is preserved so pasted text stays aligned.
    """

    # Document margin used by QTextDocument — subtract this when
    # translating widget-local pixel coords into the (col, row) cell
    # grid, otherwise every click lands one cell left of where the user
    # intended. Default is 4 px but we read it dynamically because
    # future stylesheets may override it.
    DOCUMENT_MARGIN = 4

    def __init__(
        self,
        on_input,
        on_selection_changed=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._on_input = on_input
        self._on_selection_changed = on_selection_changed
        # Cell-coord selection state. ``None`` means "no selection".
        # Anchor: where the press started. Active: where the cursor
        # currently is during a drag. These are independent so
        # ``select_all``-style ops can jump both.
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_active: tuple[int, int] | None = None
        # Selection shape: True for block (Alt-drag rectangle), False
        # for linear (default). Captured at press time so it doesn't
        # flip mid-drag.
        self._sel_block_flag: bool = False
        # Lazily set by the parent view: callable returning the live
        # pyte Screen (or None pre-start). Using a callable rather
        # than a hard reference keeps the text edit decoupled from the
        # view's lifecycle — _screen can come and go.
        self._screen_ref = lambda: None
        self._screen_width = lambda: 0
        self._screen_height = lambda: 0

        # We render via setHtml — disable the widget's own caret so the
        # pyte cursor (reversed cell) is the only visible cursor.
        self.setCursorWidth(0)
        # Read-only so Qt doesn't try to insert characters into the
        # document on text-eligible keys; our keyPressEvent override
        # intercepts BEFORE Qt's default handling so this doesn't block
        # the keys we care about.
        self.setReadOnly(True)
        # Allow the widget to receive keyboard focus and to take part in
        # tab focus chains. We previously set ``Qt.NoTextInteraction``
        # here, but that flag also makes QTextEdit refuse focus on click,
        # which is the root cause of "typing goes nowhere after I click
        # the Terminals tab". ``setReadOnly(True)`` already prevents text
        # editing; we don't need to disable interaction flags on top.
        #
        # ``TextSelectableByKeyboard`` enables Qt's no-op selection
        # machinery for screen-readers and accelerators — but our
        # selection is driven from cell coords, not Qt's machinery, so
        # this flag is benign (Qt's selection will be clobbered every
        # repaint anyway).
        self.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        # Disable the widget's own IME — the shell doesn't speak IM,
        # and the IME's preedit overlay fights with our HTML repaint.
        self.setAttribute(Qt.WA_InputMethodEnabled, False)

        # Zero the QTextDocument default margin (4 px per side). With
        # it, the rendered document ends up ``rows + 2`` cells tall
        # because the margin + our ``<pre>`` padding adds 8 px of
        # vertical chrome the cell grid doesn't need — the symptom
        # the user reported as "terminal is 2 lines taller than the
        # visible window". Setting margin to 0 makes
        # ``_pixel_to_cell`` (which already subtracts this margin) and
        # ``_recompute_size`` (which divides viewport height by
        # ``font.lineSpacing()``) agree on the cell grid boundaries.
        self.document().setDocumentMargin(0)
        self.setLineWrapMode(QTextEdit.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    # No further work here — ``setDocumentMargin(0)`` is called inline
    # in ``__init__`` (see below) so the cell grid lines up exactly
    # with the viewport pixel rect.

    # ---- selection state ---------------------------------------------------

    def selection_range(
        self,
    ) -> "tuple[tuple[int, int, int, int], bool] | None":
        """Normalized ``((x1, y1, x2, y2), is_block)`` or ``None``.

        Linear selections are normalized lexicographically so
        ``(y1, x1) ≤ (y2, x2)`` — the rect flows from the top-left
        cell to the bottom-right cell.

        Block selections carry ``(min(x), min(y), max(x), max(y))`` —
        rows cover the y-range uniformly with the same column window.
        """
        if self._sel_anchor is None or self._sel_active is None:
            return None
        (x1, y1), (x2, y2) = self._sel_anchor, self._sel_active
        if self._sel_block_flag:
            cols_lo = min(x1, x2)
            cols_hi = max(x1, x2)
            rows_lo = min(y1, y2)
            rows_hi = max(y1, y2)
            return ((cols_lo, rows_lo, cols_hi, rows_hi), True)
        if (y1, x1) > (y2, x2):
            x1, y1, x2, y2 = x2, y2, x1, y1
        if x1 == x2 and y1 == y2:
            return None
        return ((x1, y1, x2, y2), False)

    def has_selection(self) -> bool:
        return self.selection_range() is not None

    def select_all(self) -> None:
        """Select every cell on the screen (linear mode)."""
        self._sel_block_flag = False
        w = self._screen_width()
        h = self._screen_height()
        if w <= 0 or h <= 0:
            # Premature — viewport not sized yet. Park a (0,0,0,0)
            # selection that the next recompute will normalize.
            self._sel_anchor = (0, 0)
            self._sel_active = (0, 0)
        else:
            self._sel_anchor = (0, 0)
            self._sel_active = (w - 1, h - 1)
        self._emit_selection_changed()

    def clear_selection(self) -> None:
        if self._sel_anchor is None and self._sel_active is None:
            return
        self._sel_anchor = None
        self._sel_active = None
        self._sel_block_flag = False
        self._emit_selection_changed()

    def _emit_selection_changed(self) -> None:
        cb = self._on_selection_changed
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001 — best-effort hook
            pass

    # ---- scrollback ---------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Track user scroll direction to manage auto-scroll behavior."""
        super().wheelEvent(event)
        scrollbar = self.verticalScrollBar()
        parent_view = self.parent()
        if parent_view is None:
            return
        if scrollbar.value() >= scrollbar.maximum() - 5:
            parent_view._user_scrolled_up = False
        elif event.angleDelta().y() > 0:
            parent_view._user_scrolled_up = True

    # ---- input -------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        # ``_norm_mods`` handles the KeyboardModifier-flag-vs-int
        # distinction safely; without it, real Ctrl+C / Ctrl+V events
        # raise ``TypeError`` and the override silently swallows the
        # press (Qt catches the exception, marks the override
        # ``errored``, and stops calling it).
        mod_mask = _norm_mods(event.modifiers())
        ctrl = bool(mod_mask & int(Qt.ControlModifier.value))
        shift = bool(mod_mask & int(Qt.ShiftModifier.value))
        alt = bool(mod_mask & int(Qt.AltModifier.value))

        # Ctrl+Shift+C — always copy (accessibility / "copy without
        # selecting first"). Wired before the Ctrl+C branch so it
        # doesn't fall through.
        if ctrl and shift and key == Qt.Key_C:
            self._copy_selection()
            event.accept()
            return
        # Ctrl+C: copy if there's a selection; otherwise send 0x03 to
        # the PTY (kernel delivers SIGINT to the foreground process
        # group via the PTY line discipline).
        if ctrl and not shift and key == Qt.Key_C:
            if self.has_selection():
                self._copy_selection()
                event.accept()
                return
            data = b"\x03"
            self._on_input(data)
            event.accept()
            return
        # Ctrl+V and Ctrl+Shift+V — paste from system clipboard.
        if ctrl and (key == Qt.Key_V and (shift or True)):
            # We accept plain Ctrl+V too; if a Qt-level shortcut ends
            # up bound here it's because the user's just chosen to
            # live in the terminal. (Outside the terminal Ctrl+V would
            # belong to text-input widgets.)
            from PySide6.QtGui import QClipboard
            self._paste_from(mode=QClipboard.Clipboard)
            event.accept()
            return

        # Alt+= / Alt+- — adjust font size. We listen for the *key* the
        # physical key produces (Key_Equal = "+" on US, Key_Plus = "+"
        # on numpad; Key_Minus = "-", Key_Underscore = "_"/shifted-minus)
        # rather than the character, so layouts where Alt is held
        # produce different chars still hit the right branch. The
        # parent view's ``adjust_font_size`` no-ops if the change would
        # exceed the clamp; we always ``accept()`` so the key never
        # leaks through to the PTY (the shell would otherwise receive
        # ``\x1b=`` / ``\x1b-`` from the Alt prefix — confusing).
        if alt and not ctrl and not shift:
            if key in (Qt.Key_Equal, Qt.Key_Plus):
                self._adjust_font_size(+1)
                event.accept()
                return
            if key in (Qt.Key_Minus, Qt.Key_Underscore):
                self._adjust_font_size(-1)
                event.accept()
                return

        data = key_to_bytes(
            key,
            modifiers=mod_mask,
            text=event.text() or "",
        )
        if data is not None:
            self._on_input(data)
            event.accept()
            return
        # Unknown / modifier-only key — let Qt swallow it.
        event.accept()

    # ---- mouse -------------------------------------------------------------

    # Click-to-focus + start a cell selection. We can no longer swallow
    # the press (we need it for ``mouseMoveEvent`` to arrive later), so
    # we delegate to ``super().mousePressEvent`` after recording the
    # anchor. Qt's native drag selection will start too but is
    # immediately clobbered by the next ``setHtml`` repaint — the
    # visible selection is the one we drive from cell coords.
    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.pos()
        button = event.button()
        if button == Qt.LeftButton:
            # Default: linear (line-following) selection like a text
            # editor. Hold Command (Meta) or Alt for block/rectangular
            # selection (useful for columnar data like `ls -l`).
            #
            # NB: do NOT call ``super().mousePressEvent(event)`` here.
            # QTextEdit's default behaviour mutates the QTextCursor
            # to start a *native* drag selection — which draws a
            # blue highlight on top of our HTML using the document's
            # text positions (not cell coordinates). Returning from
            # here without delegating to super prevents Qt from
            # starting its native selection entirely; the cell-coord
            # selection is the only one the user sees.
            self.setFocus(Qt.MouseFocusReason)
            cell = self._pixel_to_cell(pos)
            mod_mask = _norm_mods(event.modifiers())
            self._sel_block_flag = bool(
                mod_mask & (int(Qt.MetaModifier.value) | int(Qt.AltModifier.value))
            )
            self._sel_anchor = cell
            self._sel_active = cell
            self._emit_selection_changed()
        elif button == Qt.MiddleButton:
            # PRIMARY selection paste on X11. Falls back to a no-op
            # where unsupported (Wayland / Windows).
            from PySide6.QtGui import QClipboard
            self._paste_from(mode=QClipboard.Selection)
            event.accept()
        elif button == Qt.RightButton:
            # Right-click focuses the widget too — without this,
            # action accelerators hit the previously-focused widget
            # (typically the QTabBar). ``contextMenuEvent`` reads the
            # selection from THIS widget regardless.
            self.setFocus(Qt.MouseFocusReason)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # Only react if a left-drag is in progress and we have an
        # anchor. The first mouseMove with no anchor is harmless
        # because ``mousePressEvent`` always sets the anchor at the
        # same cell as the press.
        #
        # NB: do NOT delegate to ``super().mouseMoveEvent(event)``.
        # QTextEdit's default would extend the native drag selection
        # — drawing a blue highlight across text positions that don't
        # correspond to cell coordinates. See the matching comment in
        # ``mousePressEvent`` for the full explanation.
        if (
            (event.buttons() & Qt.LeftButton)
            and self._sel_anchor is not None
        ):
            cell = self._pixel_to_cell(event.pos())
            if cell != self._sel_active:
                self._sel_active = cell
                self._emit_selection_changed()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # NB: do NOT delegate to ``super().mouseReleaseEvent(event)``.
        # See ``mousePressEvent`` for why native drag selection must
        # never get a foothold.
        if (
            event.button() == Qt.LeftButton
            and self._sel_anchor is not None
        ):
            if self._sel_active == self._sel_anchor:
                # Plain click with no drag — focus only, no selection.
                self.clear_selection()
            else:
                self._emit_selection_changed()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # Word selection: expand a click to its whitespace boundaries.
        cell = self._pixel_to_cell(event.pos())
        x, y = cell
        w = self._screen_width()
        screen = self._screen_ref()
        if not (0 <= y < self._screen_height()) or not (0 <= x < w) or screen is None:
            super().mouseDoubleClickEvent(event)
            return
        # ``screen.buffer[y][i].data`` is the established idiom from
        # ``renderer.py:207`` — read directly, no helpers needed.
        row = "".join(screen.buffer[y][i].data for i in range(w))

        def _is_word(ch: str) -> bool:
            return ch.isalnum() or ch == "_"

        x1 = x
        while x1 > 0 and _is_word(row[x1 - 1]):
            x1 -= 1
        x2 = x
        while x2 + 1 < w and _is_word(row[x2 + 1]):
            x2 += 1
        mod_mask = _norm_mods(event.modifiers())
        self._sel_block_flag = bool(
            mod_mask & (int(Qt.MetaModifier.value) | int(Qt.AltModifier.value))
        )
        self._sel_anchor = (x1, y)
        self._sel_active = (x2, y)
        self._emit_selection_changed()
        event.accept()

    # ---- viewport event interception -------------------------------------

    def viewportEvent(self, event) -> bool:  # noqa: N802
        """Override ``QAbstractScrollArea.viewportEvent`` to intercept
        mouse events on the viewport before Qt's native selection runs.

        Why this and not ``mousePressEvent``? Qt routes mouse events
        on a ``QTextEdit`` to its *viewport* child widget first. The
        QTextEdit's own ``mousePressEvent`` is only invoked by Qt's
        default scrollarea behaviour AFTER the viewport has had a
        chance to start its native drag selection. In practice
        (verified by sending real ``QMouseEvent`` instances through
        ``QApplication.sendEvent`` to the viewport) the viewport
        processes the press, starts its native selection, and never
        re-dispatches to the QTextEdit's overrides — leaving Qt's
        blue drag highlight on top of our HTML. That's the
        "selection anchored in the bottom-right" symptom.

        ``viewportEvent`` is the canonical hook for intercepting
        events going to the viewport (it's a single function that
        gets called for every event type on the viewport, including
        paint/hover/resize). Returning ``True`` consumes the event
        so Qt's native selection machinery never sees it.

        ``event.pos()`` arrives in **viewport-local** coordinates
        (origin = top-left of the viewport). We pass it straight
        through to ``mousePressEvent`` so ``_pixel_to_cell`` can
        divide by the viewport size (which is the right reference
        frame — see the matching comment in ``_pixel_to_cell``).
        We deliberately do NOT call ``mapFrom(viewport, ...)`` to
        translate to widget coords — that translation returns a
        point in an unexpected ancestor's coord system (verified
        in offscreen tests: ``mapFrom(viewport, (50, 100))`` returns
        ``(46, 37)`` instead of the expected ``(51, 101)``), and
        using the viewport size for cell math instead sidesteps the
        issue. The viewport's frame is at most 2 px on each side,
        so using viewport-local coords and viewport size gives the
        correct cell index without any translation step.
        """
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress:
            new_event = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                event.pos(),
                event.globalPosition(),
                event.button(),
                event.buttons(),
                event.modifiers(),
            )
            self.mousePressEvent(new_event)
            return True
        if etype == QEvent.Type.MouseButtonDblClick:
            new_event = QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                event.pos(),
                event.globalPosition(),
                event.button(),
                event.buttons(),
                event.modifiers(),
            )
            self.mouseDoubleClickEvent(new_event)
            return True
        if etype == QEvent.Type.MouseMove:
            # Only intercept if a drag is in progress; otherwise let
            # Qt handle hover/cursor updates.
            if event.buttons() & Qt.LeftButton:
                new_event = QMouseEvent(
                    QEvent.Type.MouseMove,
                    event.pos(),
                    event.globalPosition(),
                    event.button(),
                    event.buttons(),
                    event.modifiers(),
                )
                self.mouseMoveEvent(new_event)
                return True
            return False
        if etype == QEvent.Type.MouseButtonRelease:
            new_event = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                event.pos(),
                event.globalPosition(),
                event.button(),
                event.buttons(),
                event.modifiers(),
            )
            self.mouseReleaseEvent(new_event)
            return True
        return super().viewportEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """Right-click menu: Copy / Paste / Select All.

        First ``contextMenuEvent`` introduced anywhere in this project;
        the convention here is:

        * Menu items reflect selection state at popup time (``Copy``
          enabled iff ``has_selection()``).
        * Accelerators (``Ctrl+C`` etc.) come from each action's
          ``QKeySequence``, not from menu-bar wiring.
        * The menu lives for the lifetime of the popup only; no
          persistent items stored on the widget.
        """
        from PySide6.QtGui import QAction, QKeySequence
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QGuiApplication, QClipboard

        menu = QMenu(self)

        copy = QAction("&Copy", menu)
        copy.setShortcut(QKeySequence.Copy)
        copy.setEnabled(self.has_selection())
        copy.triggered.connect(self._copy_selection)
        menu.addAction(copy)

        paste = QAction("&Paste", menu)
        paste.setShortcut(QKeySequence.Paste)
        cb = QGuiApplication.clipboard()
        has_text = bool(cb is not None and cb.text(QClipboard.Clipboard))
        paste.setEnabled(has_text)
        paste.triggered.connect(
            lambda: self._paste_from(mode=QClipboard.Clipboard)
        )
        menu.addAction(paste)

        menu.addSeparator()

        sel_all = QAction("Select &All", menu)
        sel_all.setShortcut(QKeySequence.SelectAll)
        sel_all.triggered.connect(self.select_all)
        menu.addAction(sel_all)

        menu.exec(event.globalPos())
        event.accept()

    # ---- coordinate math ---------------------------------------------------

    def _pixel_to_cell(self, pos) -> tuple[int, int]:
        """Translate a viewport-local pixel pos → ``(col, row)``.

        ``pos`` arrives in viewport-local coordinates because
        ``viewportEvent`` is the entry point for real user clicks
        (Qt routes mouse events on a ``QTextEdit`` to its viewport
        first, before the text edit's ``mousePressEvent`` gets a
        chance to run — see that method's docstring for why).

        The cell grid is rendered edge-to-edge across the visible
        viewport (the ``<pre>`` style in the renderer fills the
        document rectangle, and we disabled scrollbars in
        ``__init__`` so there's no gutter). The cell size in pixels
        is therefore ``viewport_size / grid_size``, NOT
        ``fontMetrics.horizontalAdvance("M")`` /
        ``fontMetrics.lineSpacing()``.

        Why not font metrics? Because Qt's ``QAbstractScrollArea``
        keeps the viewport at a minimal default size until the document
        is laid out — the viewport we read in ``_recompute_size`` is
        typically 98×28 even when the widget is 800×600, so the cell
        grid's nominal pixel size (computed from those tiny viewport
        dimensions via ``_recompute_size``) gets clamped by ``MIN_COLS``
        and never updates. The cell grid the *renderer* uses is whatever
        was last accepted by ``_recompute_size`` (the 80×24 default if
        nothing else qualified), and the *visual* cell size is
        ``viewport_width / 80 × viewport_height / 24`` — neither matches
        the font metric. Using font metrics here would map a click at
        the visual centre (pixel 400, 300) to a cell that *renders* at
        pixel 500, 500 — the bottom-right corner. That's the
        "selection anchored in the bottom-right" symptom.

        ``pos`` is in viewport-local coords (``event.pos()`` from
        the viewportEvent-dispatched ``QMouseEvent``); we divide
        directly by the visible cell size. The result is clamped to
        ``[0, w-1]`` × ``[0, h-1]`` so clicks just past the last cell
        don't select the entire row.
        """
        w = self._screen_width()
        h = self._screen_height()
        if w <= 0 or h <= 0:
            # Grid not sized yet — fall back to font metrics so an
            # early click before ``start()`` doesn't divide by zero.
            # This branch only fires before the first repaint, which
            # is invisible to the user.
            fm = self.fontMetrics()
            cw = max(1, fm.horizontalAdvance("M"))
            ch = max(1, fm.lineSpacing())
            try:
                margin = self.document().documentMargin()
            except (AttributeError, RuntimeError):
                margin = self.DOCUMENT_MARGIN
            x = int((pos.x() - margin) // cw)
            y = int((pos.y() - margin) // ch)
            return (max(0, x), max(0, y))
        # Cell pixel size from the rendered viewport area. ``pos``
        # arrives in viewport-local coords (via ``viewportEvent``),
        # so we divide by the viewport's pixel dimensions to get the
        # cell index. The viewport sits inside the text edit with a
        # 1-2 px frame border, so its size is slightly smaller than
        # the text edit's own ``width()`` / ``height()`` — using the
        # widget size here would under-estimate the number of cells
        # that fit and make clicks land a few rows higher than they
        # should. The viewport size matches exactly what's visible.
        vp = self.viewport()
        vp_w = max(1, vp.width())
        vp_h = max(1, vp.height())
        cw = vp_w / w
        ch = vp_h / h
        x = int(pos.x() // cw)
        y = int(pos.y() // ch)
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        return (x, y)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)

    # ---- font size --------------------------------------------------------

    def _adjust_font_size(self, delta: int) -> None:
        """Forward a font-size change to the parent ``TerminalSessionView``.

        The text edit doesn't own the ``QFont`` — the view installed it
        in ``_build_ui`` and shares it across this widget and any other
        state that reads the font. The view's ``adjust_font_size``
        decides the clamp range and triggers the recompute + repaint
        that refits the cell grid; the text edit only routes the call.
        """
        view = self.parent()
        # ``parent()`` is a ``TerminalSessionView`` for normal usage; the
        # method only exists on it. Skip silently when running under a
        # different parent (e.g. an inline test harness) so a stray
        # Alt+= doesn't raise.
        adjust = getattr(view, "adjust_font_size", None)
        if callable(adjust):
            adjust(delta)

    # ---- clipboard primitives ---------------------------------------------

    def _copy_selection(self) -> None:
        """Copy the selection to ``QClipboard.Clipboard`` (and PRIMARY
        on X11) reading cells directly from the pyte buffer.

        Linear selections get per-line ``rstrip()`` (xterm default).
        Block selections preserve column padding so ``ls -l`` columns
        stay aligned in the paste target — that's how xterm's block
        mode behaves too.
        """
        sel = self.selection_range()
        if sel is None:
            return
        rng, is_block = sel
        text = self._extract_cells_text(rng, is_block=is_block)
        if not text:
            return
        from PySide6.QtGui import QGuiApplication, QClipboard
        cb = QGuiApplication.clipboard()
        if cb is None:
            return
        cb.setText(text, QClipboard.Clipboard)
        # Mirror to PRIMARY on X11 (xterm does this on drag-release).
        # Wayland support is spotty; ``supportsSelection`` is the
        # right gate even when Qt is built with clipboard support.
        try:
            if cb.supportsSelection():
                cb.setText(text, QClipboard.Selection)
        except AttributeError:
            # Older Qt or non-X11 build — fine, ignore.
            pass

    def _paste_from(self, *, mode) -> None:
        """Read clipboard text, sanitize, wrap, write to PTY.

        * Strips embedded ``ESC[200~`` / ``ESC[201~`` markers from
          the paste itself (paste-injection guard — without this, a
          pasted string ending in the close-marker could terminate a
          subsequent real paste mid-stream).
        * Normalizes Windows/Mac line endings (``\\r\\n`` and ``\\r``)
          to ``\\n`` so the PTY line discipline sees clean input.
        * Always wraps the payload in ``ESC[200~ ... ESC[201~``
          (bracketed-paste). This matches xterm / Alacritty /
          kitty's default behaviour for every paste: bash 5+,
          vim, fish, zsh all interpret these escapes correctly
          and treat the contents as data, while raw ``cat``/
          ``less`` ignore them (or echo them harmlessly).

          We don't gate on ``screen.mode`` because pyte doesn't
          track DEC private mode 2004 — it's screen-scoped state
          which the emulator layer intentionally doesn't model.
        """
        from PySide6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        if cb is None:
            return
        try:
            text = cb.text(mode)
        except (RuntimeError, OSError):
            return
        if not text:
            return
        # Sanitize
        text = text.replace("\x1b[201~", "").replace("\x1b[200~", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        data = b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~"
        self._on_input(data)

    def _extract_cells_text(
        self,
        rng: tuple[int, int, int, int],
        *,
        is_block: bool,
    ) -> str:
        """Walk the pyte buffer for the cell range, return joined text.

        Linear: each row's range is x-clamped per-row (full-width
        rows between first and last selected rows). Each line has
        trailing whitespace stripped; trailing all-blank lines
        removed. Matches xterm's clipboard output.

        Block: every row gets the same x1..x2 window. Right padding
        preserved so columns stay aligned in the target. Trailing
        fully-blank lines still removed for parity.
        """
        screen = self._screen_ref()
        if screen is None:
            return ""
        x1, y1, x2, y2 = rng
        rows = screen.lines
        cols = screen.columns
        # Clamp to the live screen — selection state may predate a
        # resize (we clear on resize, but a race between keypress and
        # recompute could otherwise produce out-of-range reads).
        x1 = max(0, min(cols - 1, x1))
        y1 = max(0, min(rows - 1, y1))
        x2 = max(0, min(cols - 1, x2))
        y2 = max(0, min(rows - 1, y2))
        out_lines: list[str] = []
        if is_block:
            for y in range(y1, y2 + 1):
                line = "".join(
                    screen.buffer[y][x].data for x in range(x1, x2 + 1)
                )
                out_lines.append(line)
            # Drop trailing fully-blank rows so copy doesn't end with \n.
            while out_lines and not out_lines[-1].strip():
                out_lines.pop()
            return "\n".join(out_lines)
        # Linear: full-width rows between y1 and y2; first row from
        # x1 to end; last row from 0 to x2.
        for y in range(y1, y2 + 1):
            xa = x1 if y == y1 else 0
            xb = cols - 1 if y == y2 else x2
            line = "".join(
                screen.buffer[y][x].data for x in range(xa, xb + 1)
            )
            out_lines.append(line.rstrip())
        while out_lines and not out_lines[-1]:
            out_lines.pop()
        return "\n".join(out_lines)


class TerminalSessionView(QWidget):
    """One live shell session as a Qt widget.

    Public surface
    --------------
    - ``start()`` — fork the child PTY and begin reading.
    - ``shutdown()`` — idempotent teardown; safe to call multiple times.
    - ``title()`` — short label suitable for the tab text.
    - ``exited`` — signal emitted when the child shell exits.

    The view also installs a small ``QTimer`` that coalesces repaints to
    ~16 ms (60 fps) so a flood of PTY bytes doesn't drown the GUI in
    ``setHtml`` calls.
    """

    # Emitted once, when the child shell exits. After this the view is
    # read-only and the tab label should change to ``"<title> (exited)"``.
    exited = Signal()

    # Repaint interval in ms. Coalesce multiple reads into one HTML
    # render so a `yes` flood doesn't pin a CPU.
    REPAINT_INTERVAL_MS = 16

    # Resize-debounce in ms. Kept separate from ``REPAINT_INTERVAL_MS``
    # because each fired resize triggers a ``TIOCSWINSZ`` ioctl that
    # wakes the child shell to redraw. A window-drag firing 60Hz of
    # those is wasteful; 50ms collapses to ~20/sec which is invisible
    # to the user but cheap for the shell.
    RESIZE_DEBOUNCE_MS = 50

    # Default terminal size in cells until the widget has been laid out.
    DEFAULT_ROWS = 24
    DEFAULT_COLS = 80

    # Minimum dimensions we trust the layout to compute. Below these we
    # keep whatever size we already have. See ``_recompute_size``.
    MIN_COLS = 40
    MIN_ROWS = 10

    # Font-size clamp in points. Below 8pt the cell grid becomes
    # unreadable on a HiDPI display; above 32pt one row consumes the
    # entire viewport on a stock 800x600 window. ``_build_ui`` sets the
    # initial size to ``QFontDatabase.systemFont(QFontDatabase.FixedFont)``
    # which is usually 10-13pt — comfortably inside the clamp.
    MIN_FONT_POINT_SIZE = 8
    MAX_FONT_POINT_SIZE = 32

    def __init__(
        self,
        argv: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        session_label: str = "terminal",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._argv = argv
        self._session_label = session_label
        # Per-session environment overrides. ``env`` is a COMPLETE
        # replacement (PtySession calls ``os.execvpe``), so callers must
        # build on top of ``os.environ.copy()`` themselves — see
        # ``src/profiles/env_builder.build_claude_session_env`` for the
        # Claude-side helper. ``None`` (the default) means inherit the
        # parent GUI process's environment verbatim.
        self._env = env
        # Per-session working directory. ``None`` means inherit cwd.
        # PtySession does ``os.chdir(self._cwd)`` in the forked child
        # before exec, so this is observed by the spawned process but
        # does not affect the GUI process.
        self._cwd = cwd
        self._started = False
        self._stopped = False
        self._exited = False
        self._rows = self.DEFAULT_ROWS
        self._cols = self.DEFAULT_COLS

        # pyte state — created in start().
        self._screen = None
        self._stream = None
        # The PtySession is created in start(); declared here so type
        # checkers don't complain in shutdown().
        self._session = None  # type: ignore[assignment]
        # Repaint debounce timer.
        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(self.REPAINT_INTERVAL_MS)
        self._repaint_timer.timeout.connect(self._repaint)

        # Resize debounce timer — keeps _recompute_size + ioctl from
        # firing at 60Hz during a window-drag. See RESIZE_DEBOUNCE_MS
        # for the rationale; paired with ``resizeEvent`` and a settle
        # pass in ``start()`` so the first sizing isn't a tab-strip
        # placeholder.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(self.RESIZE_DEBOUNCE_MS)
        self._resize_timer.timeout.connect(self._on_resize_debounced)

        # Session recorder — None until start_recording() is called.
        self._recorder = None

        # Plain-text session logger — started automatically if logging
        # is enabled (controlled by the status-bar checkbox via QSettings).
        self._logger = None

        # Scrollback: when True, user has scrolled up and we should NOT
        # auto-scroll to the bottom on repaint.
        self._user_scrolled_up = False

        # Lazy pyte import — pyte is a runtime dep but not required to
        # construct the widget, so render-only tests can still import this
        # module.
        import pyte  # noqa: F401 — imported for type hints only

        self._build_ui()

    # ---- public API ---------------------------------------------------------

    def start(self) -> None:
        """Fork the child shell and wire up the reader signals."""
        if self._started:
            return
        self._started = True

        # Apply the saved terminal colour theme to the renderer.
        from src.ui.terminal.settings_dialog import get_terminal_theme
        from src.ui.terminal.renderer import apply_theme
        apply_theme(get_terminal_theme())

        # Compute initial size from the widget's current pixel size if
        # it's been laid out; otherwise fall back to defaults.
        self._recompute_size()

        # Schedule one settle pass: by the time this fires, the widget
        # has been parented and laid out at its real size, so
        # _recompute_size will see the viewport dimensions instead of
        # the tab-strip placeholder. Without this the terminal would
        # freeze at its post-_recompute_size defaults forever.
        QTimer.singleShot(0, self._resize_timer.start)

        import pyte

        self._screen = pyte.HistoryScreen(self._cols, self._rows, history=5000)
        self._stream = pyte.ByteStream(self._screen)

        from src.ui.terminal.pty_backend import PtySession

        self._session = PtySession(
            argv=self._argv,
            rows=self._rows,
            cols=self._cols,
            env=self._env,
            cwd=self._cwd,
            parent=self,
        )
        self._session.data_received.connect(self._on_data)
        self._session.process_exited.connect(self._on_exited)
        self._session.read_error.connect(self._on_read_error)
        self._session.start_pty()

        # Start plain-text logging if enabled in settings.
        self._start_logging_if_enabled()

        # First paint — usually empty until the shell prompt arrives.
        self._repaint()

    def shutdown(self) -> None:
        """Idempotent. Safe before start, after start, and multiple times."""
        if self._stopped:
            return
        self._stopped = True
        self._repaint_timer.stop()
        if self._recorder is not None and self._recorder.is_recording:
            self._recorder.stop()
        if self._logger is not None and self._logger.is_enabled:
            self._logger.stop()
        if self._session is not None:
            try:
                self._session.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def start_recording(self, path: str | None = None) -> str:
        """Start recording this session to an asciicast v2 file.

        Returns the path the recording is saved to.
        """
        from pathlib import Path
        from src.ui.terminal.session_recorder import SessionRecorder

        if path is None:
            import time
            recordings_dir = Path.home() / ".sharkdrown" / "recordings"
            recordings_dir.mkdir(parents=True, exist_ok=True)
            path = str(recordings_dir / f"session_{int(time.time())}.cast")

        self._recorder = SessionRecorder(
            path=path, cols=self._cols, rows=self._rows
        )
        self._recorder.start()
        return str(self._recorder.path)

    def stop_recording(self) -> str | None:
        """Stop recording and return the path, or None if not recording."""
        if self._recorder is None or not self._recorder.is_recording:
            return None
        self._recorder.stop()
        path = str(self._recorder.path)
        self._recorder = None
        return path

    @property
    def is_recording(self) -> bool:
        return self._recorder is not None and self._recorder.is_recording

    # ---- Session logging ---------------------------------------------------

    def _start_logging_if_enabled(self) -> None:
        """Start the plain-text logger if QSettings says logging is on."""
        from PySide6.QtCore import QSettings
        from src.ui.terminal.session_logger import SessionLogger

        settings = QSettings("SharkDrown", "App")
        enabled = settings.value("session_logging_enabled", True, type=bool)
        if enabled:
            self._logger = SessionLogger(session_label=self._session_label)
            self._logger.start()

    def set_logging_enabled(self, enabled: bool) -> None:
        """Enable or disable logging for this session."""
        from src.ui.terminal.session_logger import SessionLogger

        if enabled and (self._logger is None or not self._logger.is_enabled):
            self._logger = SessionLogger(session_label=self._session_label)
            self._logger.start()
        elif not enabled and self._logger is not None and self._logger.is_enabled:
            self._logger.stop()

    @property
    def is_logging(self) -> bool:
        return self._logger is not None and self._logger.is_enabled

    def title(self) -> str:
        """Short label suitable for the tab text."""
        name = (self._argv or ["/bin/bash"])[0]
        # Show only the basename.
        name = name.rsplit("/", 1)[-1]
        if self._exited:
            return f"{name} (exited)"
        return name

    def focus_terminal(self) -> None:
        """Move keyboard focus to this session's text edit.

        Called from MainWindow when the Terminals tab becomes active and
        from TerminalContainerView when a new session is opened. Without
        this, focus stays on the QTabBar (which interprets keystrokes as
        tab-navigation shortcuts) or on the toolbar's New Session button
        — in both cases the user's typing never reaches the PTY.
        """
        # setFocus() on a widget whose window isn't yet active can be a
        # no-op in some Qt builds; setting focus on both the widget and
        # its window is the safest pattern.
        self.text_edit.setFocus(Qt.OtherFocusReason)
        if self.window() is not None:
            self.window().activateWindow()

    # ---- font size --------------------------------------------------------

    def adjust_font_size(self, delta: int) -> None:
        """Bump the text edit's font point-size by ``delta`` (typically ±1).

        Wired to ``Alt+=`` / ``Alt+-`` from the text edit's
        ``keyPressEvent``. Clamps to ``MIN_FONT_POINT_SIZE`` /
        ``MAX_FONT_POINT_SIZE`` so the cell grid doesn't collapse to
        unreadable 4pt text or balloon into 96pt. After the change we
        immediately recompute rows/cols and forward the new size to
        the shell via ``TIOCSWINSZ`` — otherwise the visible grid
        wouldn't match what bash sees and prompts would wrap at the
        wrong column.
        """
        if delta == 0:
            return
        current = self._base_font.pointSize()
        # ``QFont.pointSize()`` returns -1 when the size was set in
        # pixels (a pixelSize, not a pointSize). We always set points
        # in ``_build_ui`` so this shouldn't happen in practice, but
        # fall back to ``pixelSize // 1`` if it does so the user
        # still gets *some* adjustment instead of a silent no-op.
        if current <= 0:
            current = self._base_font.pixelSize() or 10
        new_size = max(
            self.MIN_FONT_POINT_SIZE,
            min(self.MAX_FONT_POINT_SIZE, current + delta),
        )
        if new_size == current:
            # Already at the clamp — no visible change, but accept()
            # in the caller has already eaten the keystroke so the
            # shell doesn't see a stray ESC= either.
            return
        self._base_font.setPointSize(new_size)
        # ``setFont`` updates the text edit's metrics immediately; the
        # next ``_recompute_size`` call sees the new cell dimensions.
        self.text_edit.setFont(self._base_font)
        # Drop any held selection — cell coordinates from the old
        # grid no longer map to the same pixels after reflow.
        if self.text_edit.has_selection():
            self.text_edit.clear_selection()
        # Repaint so the new size renders. The size hint + geometry
        # will settle on the next resize pass; ``_recompute_size``
        # reads the freshly-updated font metrics.
        self._recompute_size()
        # Forward the new pixel-grid dimensions to the shell so its
        # line editor knows the new columns and wraps prompts
        # correctly. Without this, ``echo $COLUMNS`` would still
        # report the old value after the font change.
        if self._session is not None and self._started and not self._exited:
            self._session.resize(self._rows, self._cols)
        self._repaint()

    # ---- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.text_edit = TerminalTextEdit(
            on_input=self._send_input,
            on_selection_changed=self._schedule_repaint,
            parent=self,
        )
        # Wire lazy screen references so the text edit can read cells
        # (for copy) and probe ``screen.mode`` (for bracketed-paste)
        # without holding a second live ref to ``pyte.Screen``.
        self.text_edit._screen_ref = lambda: self._screen
        self.text_edit._screen_width = lambda: self._cols
        self.text_edit._screen_height = lambda: self._rows
        # Use a monospace font. Prefer the user's saved terminal font;
        # fall back to the system monospace family.
        from src.ui.terminal.settings_dialog import load_terminal_settings
        ts = load_terminal_settings()
        if ts["font_family"]:
            from PySide6.QtGui import QFont
            self._base_font = QFont(ts["font_family"])
        else:
            self._base_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        size = ts["font_size"] if ts["font_size"] > 0 else max(self._base_font.pointSize(), 10)
        self._base_font.setPointSize(size)
        self.text_edit.setFont(self._base_font)
        # Disable the horizontal scrollbar — the screen is fixed-width
        # and any overflow is a bug, not user-visible state.
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.text_edit)

        # Focus the text edit so it captures keypresses.
        self.text_edit.setFocus()

    # ---- input / output ----------------------------------------------------

    def _send_input(self, data: bytes) -> None:
        if self._session is None or self._exited:
            return
        self._session.write(data)

    def _on_data(self, chunk: bytes) -> None:
        if self._stream is None:
            return
        if self._recorder is not None and self._recorder.is_recording:
            self._recorder.record(chunk)
        if self._logger is not None and self._logger.is_enabled:
            self._logger.write(chunk)
        # Detect a scroll BEFORE feeding bytes so we know if content
        # reflowed. After the feed, if the screen scrolled (cursor at
        # top + dirty rows above), any held cell-coord selection now
        # refers to different characters, so drop it. This matches
        # xterm's behaviour: a selection made before output arrives is
        # cleared once the screen scrolls past it.
        cursor_y_before = -1
        if self._screen is not None:
            try:
                cursor_y_before = int(self._screen.cursor.y)
            except (AttributeError, TypeError, ValueError):
                cursor_y_before = -1
        self._stream.feed(chunk)
        if (
            self._screen is not None
            and cursor_y_before >= 0
            and self._screen.cursor.y == 0
            and hasattr(self, "text_edit")
            and self.text_edit.has_selection()
        ):
            self.text_edit.clear_selection()
        self._schedule_repaint()

    def _on_exited(self, _code: int) -> None:
        self._exited = True
        self.exited.emit()

    def _on_read_error(self, msg: str) -> None:
        # Surface as a status line at the bottom of the screen. pyte
        # doesn't have a "system message" channel, so we just append to
        # the input buffer; the shell may or may not redraw, but at
        # least the bytes go somewhere observable.
        # In practice this fires on EIO/EPIPE which is followed
        # immediately by process_exited — so we let _on_exited handle
        # the user-visible state.
        if not self._exited:
            self._on_exited(0)

    # ---- repaint / resize --------------------------------------------------

    def _schedule_repaint(self) -> None:
        if not self._repaint_timer.isActive():
            self._repaint_timer.start()

    def _repaint(self) -> None:
        if self._screen is None:
            return
        # Pass the cell-coordinate selection through to the renderer so
        # the highlighted cells render with reversed fg/bg.
        sel = None
        if hasattr(self, "text_edit"):
            sel = self.text_edit.selection_range()

        # Build full HTML: scrollback history + live screen.
        live_html = screen_to_html(self._screen, selection=sel)

        history_html = ""
        if hasattr(self._screen, "history") and self._screen.history.top:
            history_html = history_lines_to_html(
                self._screen.history.top, self._screen.columns
            )

        if history_html:
            # Inject history rows before the live content inside the <pre> tag.
            # screen_to_html returns '<pre style="...">CONTENT</pre>'.
            # We insert history_html right after the opening <pre ...> tag.
            pre_end = live_html.index(">") + 1
            html = live_html[:pre_end] + history_html + live_html[pre_end:]
        else:
            html = live_html

        # Detect scroll position before replacing content.
        scrollbar = self.text_edit.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5

        self.text_edit.setHtml(html)

        # Auto-scroll to the cursor unless the user manually scrolled up.
        if not self._user_scrolled_up or was_at_bottom:
            # Even when ``_user_scrolled_up`` was True at the start of
            # this repaint, a repaint that already detected the user
            # at the bottom (the ``was_at_bottom`` short-circuit) should
            # re-engage follow mode — the user can navigate back to
            # the bottom via keyboard (Ctrl+End), the scrollbar drag
            # handle, or a programmatic scroll, none of which fire
            # ``wheelEvent`` and so can't clear ``_user_scrolled_up``
            # on their own. Without this, a user who scrolled up
            # history and then scrolled back down would see new
            # output accumulate invisibly below the viewport — the
            # "active line off the bottom" symptom.
            self._user_scrolled_up = False
            # Force the document layout so ``scrollbar.maximum()``
            # reflects the new content height. We pin the document's
            # text width to the viewport first: without that,
            # ``adjustSize`` is a no-op for documents with no
            # fixed-width blocks (ours is a flowing ``<pre>``), and
            # ``scrollbar.maximum()`` stays at the value left over
            # from the *previous* repaint.
            viewport_width = self.text_edit.viewport().width()
            if viewport_width > 0:
                self.text_edit.document().setTextWidth(viewport_width)
            self.text_edit.document().adjustSize()

            # Scroll to the *cursor's* position in the document, not
            # to the bottom of the document. For an active session
            # the cursor sits at the bottom of the live screen (which
            # is at the bottom of the document), so the two are
            # equivalent. For a new session with no data, the
            # cursor is at the very top — ``setValue(maximum)`` would
            # land the user on the blank rows below the prompt and
            # leave the active line "a few lines off the top" of the
            # viewport (the symptom the user reported after the
            # previous fix). ``ensureCursorVisible`` is also more
            # robust than ``setValue(maximum)`` when the document is
            # a few pixels taller than expected: Qt's layout engine
            # knows exactly where the cursor is and scrolls to put
            # it in the visible area, even if ``maximum`` is off by
            # one row's worth of pixels.
            text_cursor = self.text_edit.textCursor()
            text_cursor.movePosition(QTextCursor.Start)
            # The pyte cursor's row is the offset within the live
            # screen; the live screen starts after any scrollback
            # history. Walk the QTextCursor down that many lines so
            # ``ensureCursorVisible`` lands on the cursor's row.
            history_lines = 0
            if hasattr(self._screen, "history") and self._screen.history.top:
                history_lines = len(self._screen.history.top)
            target_line = history_lines + int(self._screen.cursor.y)
            for _ in range(target_line):
                text_cursor.movePosition(QTextCursor.Down)
            self.text_edit.setTextCursor(text_cursor)
            self.text_edit.ensureCursorVisible()
            # ``ensureCursorVisible`` is a *minimum-distance* scroll: it
            # scrolls only enough to bring the cursor's rect into the
            # viewport. For a terminal that's correct for the cursor
            # itself, but the content *below* the cursor (Claude Code's
            # status bar / latest output, which sits on the last few
            # rows of the live screen while the cursor is parked a few
            # rows above it) ends up below the visible area — the
            # "active line reaches the bottom, last five lines aren't
            # viewable" symptom during an active session.
            #
            # Only force ``setValue(maximum)`` when there's *real*
            # overflow — i.e. scrollback history exists. A fresh
            # session has ``history_lines == 0`` and the document is
            # at most a few rows taller than the viewport (rounding
            # noise from ``_measure_rendered_rows``); snapping to
            # ``maximum`` there scrolls the TUI header off the top
            # because the cursor (already visible to ``ensureCursorVisible``)
            # sits in the bottom viewport-height. The
            # ``history_lines > 0`` gate means we only snap once
            # content has actually scrolled off the top of the live
            # screen — which is exactly when the user wants to see
            # the latest output rather than the session start.
            if history_lines > 0:
                if scrollbar.value() < scrollbar.maximum():
                    scrollbar.setValue(scrollbar.maximum())
            # Reset horizontal scroll — ``ensureCursorVisible`` may
            # have scrolled right to keep the cursor's column in
            # view, clipping the first characters on the left. The
            # terminal grid should always start at x=0.
            hbar = self.text_edit.horizontalScrollBar()
            if hbar.value() != 0:
                hbar.setValue(0)

    def _recompute_size(self) -> None:
        """Translate current pixel size → rows/cols via QFontMetrics.

        Skipped when:
          - The widget hasn't been laid out yet (``viewport().width() <= 0``).
          - The widget is suspiciously small (under ``MIN_COLS`` cells wide
            or ``MIN_ROWS`` cells tall). This can happen transiently during
            the very first layout pass — Qt sometimes shows a tab at
            ~12×28 px for one frame before the splitter sizes settle. If
            we resize the pyte Screen to 1×12 in that window, the shell
            sees a tiny terminal and may not produce a prompt, which the
            user experiences as "the terminal shows nothing".

        In both skip cases we keep the current ``self._rows/cols`` so the
        shell continues with whatever size it already has.

        Column width and row height are calibrated against the
        **rendered** QTextDocument, not just ``font.lineSpacing()``.
        QTextDocument's layout engine renders each ``\n``-separated
        line ~1 px taller than ``QFontMetrics`` reports, because the
        layout includes the document's bottom inset that the metrics
        API doesn't. Without this calibration the document would be
        ``rows + 2`` cells tall at 24 rows — that's the "terminal
        shows 2 extra lines of phantom scroll-back" symptom.
        """
        fm = self.text_edit.fontMetrics()
        cw = max(1, fm.horizontalAdvance("M"))
        ch = max(1, fm.lineSpacing())
        w = self.text_edit.viewport().width()
        h = self.text_edit.viewport().height()
        if w <= 0 or h <= 0:
            return
        # Use floor division then verify the total pixel width fits.
        # Without this check, rounding means cols*cw can exceed the
        # viewport by up to (cw-1) px, pushing content past the right
        # edge. ensureCursorVisible() then scrolls horizontally to show
        # the cursor — clipping the first characters on the left.
        cols = max(1, w // cw)
        if cols * cw > w:
            cols -= 1
        # Measure the ACTUAL rendered line height by feeding ``rows``
        # candidate newlines into a temporary document with the same
        # font + the same line-height/pre rules the renderer uses.
        # This is the only way to know exactly how many ``\n``-rows
        # the viewport can hold; ``font.lineSpacing()`` is off by 1-2
        # px/row.
        rows = self._measure_rendered_rows(h)
        if rows < 1:
            rows = max(1, h // ch)
        # Reject implausibly small sizes — we'd rather keep a known-good
        # 24×80 (or whatever the last good size was) than resize the shell
        # to 1×12 for one frame.
        if cols < self.MIN_COLS or rows < self.MIN_ROWS:
            return
        if rows == self._rows and cols == self._cols:
            return
        self._rows = rows
        self._cols = cols
        if self._screen is not None:
            self._screen.resize(rows, cols)
        if self._session is not None:
            self._session.resize(rows, cols)

    def _measure_rendered_rows(self, viewport_height_px: int) -> int:
        """Return how many cell-rows ``viewport_height_px`` actually holds.

        Wraps the same ``screen_to_html`` rendering through a temporary
        QTextDocument with the widget's font, then measures the result.
        Iterates from a generous upper bound down to the largest
        ``n`` whose rendered height is ``<= viewport_height_px``;
        returns 0 only if even one row doesn't fit (caller falls back
        to the integer-division heuristic).
        """
        from PySide6.QtGui import QTextDocument

        if viewport_height_px <= 0:
            return 0
        # Use the same font the text edit is using — different fonts
        # produce wildly different rendered heights.
        doc = QTextDocument(self)
        doc.setDocumentMargin(0)
        # Mirror the renderer's <pre> style:
        doc.setDefaultStyleSheet(
            "pre { "
            "  font-family:Menlo,Consolas,monospace; "
            "  margin:0;padding:0;line-height:1.0; "
            "}"
        )
        doc.setDefaultFont(self.text_edit.font())
        # Probe from a known-safe upper bound. ``viewport_height //
        # lineSpacing`` is generous; trim down until ``doc_height <=
        # viewport_height``.
        upper = max(self.MIN_ROWS, viewport_height_px // max(1, self.text_edit.fontMetrics().lineSpacing())) + 4
        upper = min(upper, 500)  # sanity cap
        # Build a sample payload of ``n`` rows of plain text — the
        # renderer's ``<pre>`` does the heavy lifting; for measurement
        # purposes a row of spaces is enough.
        best = 0
        # Binary-search-ish trim: start at ``upper`` and step down
        # while the rendered height fits. Brute step is fine here —
        # this runs once per resize event, not in a hot loop.
        for n in range(upper, 0, -1):
            # ``n`` rows = ``n-1`` explicit newlines plus the trailing
            # line Qt always renders.
            text = ("\n" * (n - 1)) + " "
            doc.setPlainText(text)
            doc.setPlainText(text)  # second call forces re-layout
            h = doc.size().height()
            if h <= viewport_height_px:
                best = n
                break
        return best

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Two timers for two concerns:
        #  - _resize_timer (50ms) for recompute_size + TIOCSWINSZ
        #  - _repaint_timer (16ms) for fast visual feedback (text reflows)
        # Reusing _repaint_timer for sizing would mean firing the kernel
        # ioctl at 60Hz during a window-drag; the dedicated debounce keeps
        # that bounded to ~20Hz while still feeling instant to the user.
        if not self._resize_timer.isActive():
            self._resize_timer.start()
        if not self._repaint_timer.isActive():
            self._repaint_timer.start()

    def _on_resize_debounced(self) -> None:
        """Run after RESIZE_DEBOUNCE_MS of resize inactivity.

        Recomputes rows/cols, forwards the new size to the kernel, then
        forces a repaint so the visible grid matches the actual screen
        dimensions. If the screen size actually changed we drop any held
        selection (cell coordinates no longer reference the same
        content after a reflow).
        """
        before = (self._rows, self._cols)
        self._recompute_size()
        after = (self._rows, self._cols)
        # Selection lives in our state by cell coords — those point at
        # different characters after a reflow, so clear them.
        if before != after and hasattr(self, "text_edit"):
            if getattr(self.text_edit, "has_selection", lambda: False)():
                self.text_edit.clear_selection()
        self._repaint()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.text_edit.fontMetrics() if hasattr(self, "text_edit") else None
        if fm is None:
            return super().sizeHint()
        return QSize(fm.horizontalAdvance("M") * 80, fm.lineSpacing() * 24)
