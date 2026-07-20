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

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QKeyEvent,
    QTextCursor,
)
from PySide6.QtWidgets import QTextEdit, QVBoxLayout, QWidget

from src.ui.terminal.renderer import screen_to_html


# --- Pure keymap (testable without Qt) -------------------------------------

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
    before the bitmask comparisons.
    """
    if modifiers is None:
        mod_mask = 0
    elif isinstance(modifiers, int):
        mod_mask = modifiers
    else:
        # ``Qt.KeyboardModifier`` flag enum — extract the underlying int.
        mod_mask = int(modifiers.value)
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
    * **Linear** (default): drag flows across rows. ``Ctrl+C`` with a
      linear selection copies; ``Ctrl+C`` with no selection sends
      SIGINT (0x03) to the PTY.
    * **Block** (Alt-drag): rectangle with the same column range across
      all touched rows. Useful for cutting columns out of tabular
      output like ``ls -l``.
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

    # ---- input -------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(int(mods) & int(Qt.ControlModifier.value))
        shift = bool(int(mods) & int(Qt.ShiftModifier.value))

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

        data = key_to_bytes(
            key,
            modifiers=mods,
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
            # Click = focus + start selection. The Alt key determines
            # block-vs-linear; capture at press time.
            self.setFocus(Qt.MouseFocusReason)
            cell = self._pixel_to_cell(pos)
            self._sel_block_flag = bool(
                int(event.modifiers()) & int(Qt.AltModifier.value)
            )
            self._sel_anchor = cell
            self._sel_active = cell
            super().mousePressEvent(event)
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
        if (
            (event.buttons() & Qt.LeftButton)
            and self._sel_anchor is not None
        ):
            cell = self._pixel_to_cell(event.pos())
            if cell != self._sel_active:
                self._sel_active = cell
                self._emit_selection_changed()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.LeftButton
            and self._sel_anchor is not None
        ):
            if self._sel_active == self._sel_anchor:
                # Plain click with no drag — focus only, no selection.
                self.clear_selection()
            else:
                self._emit_selection_changed()
        super().mouseReleaseEvent(event)

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
        self._sel_block_flag = False
        self._sel_anchor = (x1, y)
        self._sel_active = (x2, y)
        self._emit_selection_changed()
        event.accept()

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
        """Translate a widget-local pixel pos → ``(col, row)``.

        Subtracts the QTextDocument margin (4 px by default) so clicks
        line up with rendered cells. ``pos`` is in widget-local coords;
        the viewport is a child we map through.
        """
        vp = self.viewport()
        fm = self.fontMetrics()
        cw = max(1, fm.horizontalAdvance("M"))
        ch = max(1, fm.lineSpacing())
        try:
            margin = self.document().documentMargin()
        except Exception:  # noqa: BLE001
            margin = self.DOCUMENT_MARGIN
        local = vp.mapFromParent(pos)
        x = int((local.x() - margin) // cw)
        y = int((local.y() - margin) // ch)
        w = self._screen_width()
        h = self._screen_height()
        if w > 0:
            x = max(0, min(w - 1, x))
        else:
            x = 0
        if h > 0:
            y = max(0, min(h - 1, y))
        else:
            y = 0
        return (x, y)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)

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
        except Exception:  # noqa: BLE001
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

    def __init__(self, argv: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._argv = argv
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

        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)

        from src.ui.terminal.pty_backend import PtySession

        self._session = PtySession(
            argv=self._argv,
            rows=self._rows,
            cols=self._cols,
            parent=self,
        )
        self._session.data_received.connect(self._on_data)
        self._session.process_exited.connect(self._on_exited)
        self._session.read_error.connect(self._on_read_error)
        self._session.start_pty()

        # First paint — usually empty until the shell prompt arrives.
        self._repaint()

    def shutdown(self) -> None:
        """Idempotent. Safe before start, after start, and multiple times."""
        if self._stopped:
            return
        self._stopped = True
        self._repaint_timer.stop()
        if self._session is not None:
            try:
                self._session.shutdown()
            except Exception:  # noqa: BLE001
                pass

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
        # Use a monospace font. Pick the system monospace family.
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(max(font.pointSize(), 10))
        self.text_edit.setFont(font)
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
            except Exception:  # noqa: BLE001
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
        # the highlighted cells render with reversed fg/bg. ``selection_range``
        # returns ``((x1,y1,x2,y2), is_block)`` or None — exactly what
        # ``screen_to_html`` expects.
        sel = None
        if hasattr(self, "text_edit"):
            sel = self.text_edit.selection_range()
        html = screen_to_html(self._screen, selection=sel)
        # setHtml is cheap on a fixed-size grid but it RESETS the scroll
        # position to the top of the document. Without an explicit
        # ensureCursorVisible() call the user would see the screen snap
        # back to the top on every repaint — which looks like "the cursor
        # jumped back" when typing. Move the cursor to End AND scroll to
        # it so the latest output (and the pyte cursor cell) stays in
        # view at the bottom.
        self.text_edit.setHtml(html)
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()

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
        """
        fm = self.text_edit.fontMetrics()
        cw = max(1, fm.horizontalAdvance("M"))
        ch = max(1, fm.lineSpacing())
        w = self.text_edit.viewport().width()
        h = self.text_edit.viewport().height()
        if w <= 0 or h <= 0:
            return
        cols = max(1, w // cw)
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
