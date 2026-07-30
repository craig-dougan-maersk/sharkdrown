"""Bottom-left status-bar widget that renders the user's world clocks.

One ``QLabel`` per clock, separated by thin vertical ``QFrame``
dividers, ticking every second via a child ``QTimer``. The widget
is hidden entirely when:

- :attr:`ClockCollection.enabled` is ``False``, **or**
- :attr:`ClockCollection.clocks` is empty.

The widget is otherwise always visible (per the spec — clocks live
on the bottom-left regardless of which top-level tab is active).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from src.ui.clocks.models import ClockCollection, load_clocks


# Tick interval (ms). 1s is enough for a seconds-resolution display;
# sub-second ticks would just consume battery on a desktop session.
_TICK_MS = 1000


class ClockToolbarWidget(QWidget):
    """Horizontal row of clocks in the bottom-left status bar.

    Public API
    ----------
    :meth:`refresh` — re-read ``QSettings`` and rebuild the labels.
    :meth:`collection` — return the currently-rendered collection
        (mostly for tests; the toolbar widget doesn't mutate the
        stored collection itself).
    :meth:`stop_timer` — stop the ticker (called from
        ``MainWindow.closeEvent`` so the timer doesn't outlive the
        window).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # No expanding stretch — the widget should hug its content so
        # the status bar gives it just the space it needs.
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 0, 4, 0)
        self._layout.setSpacing(6)

        # Cache the collection the widget is currently displaying so
        # ``collection()`` doesn't have to round-trip QSettings.
        self._collection: ClockCollection = ClockCollection()
        # ``_clock_labels[i]`` mirrors ``_collection.clocks[i]`` by
        # position. ``_divider_labels[i]`` is the divider *before*
        # clock ``i`` — index 0 has no divider (no clock before it).
        self._clock_labels: list[QLabel] = []
        self._divider_labels: list[QFrame] = []

        # Child timer — ``self`` as parent so it dies with the widget.
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # Initial render — empty collection → hidden.
        self._rebuild_labels()

    # ---- public API ----------------------------------------------------

    def refresh(self) -> None:
        """Re-read ``QSettings`` and rebuild the labels.

        Called by ``MainWindow._open_clocks_dialog`` after the user
        commits changes in the manager dialog. Also called once at
        startup from ``_init_status_bar`` so the widget reflects the
        persisted state on first show.
        """
        self._collection = load_clocks()
        self._rebuild_labels()
        self._tick()  # render immediately rather than waiting a tick

    def collection(self) -> ClockCollection:
        """Return the collection currently rendered. For tests."""
        return self._collection

    def stop_timer(self) -> None:
        """Stop the per-second ticker. Idempotent."""
        if self._timer.isActive():
            self._timer.stop()

    # ---- internals -----------------------------------------------------

    def _rebuild_labels(self) -> None:
        """Recreate ``QLabel`` / divider widgets from the current collection.

        Called whenever the collection changes (refresh) or after
        edits. Clears the layout first so old widgets don't leak —
        Qt's parent-child ownership would GC them, but explicit
        removal makes the structure obvious.
        """
        # Drop everything currently in the layout.
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._clock_labels = []
        self._divider_labels = []

        enabled = self._collection.enabled
        clocks = self._collection.clocks

        # Hidden when disabled or no clocks. ``setVisible(False)`` on
        # a status-bar child removes it from the visible layout
        # without destroying the widget (the ticker keeps running so
        # toggling back on shows the current time immediately).
        self.setVisible(enabled and bool(clocks))

        mono_family = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont
        ).family()

        for i, clock in enumerate(clocks):
            label = QLabel(self)
            label.setTextFormat(Qt.TextFormat.PlainText)
            font = label.font()
            font.setFamily(mono_family)
            font.setPointSize(11)
            label.setFont(font)
            label.setStyleSheet("color: gray; padding: 0 2px;")
            label.setToolTip(
                f"{clock.timezone} ({'24h' if clock.use_24h else '12h'})"
            )
            self._clock_labels.append(label)
            if i > 0:
                divider = QFrame(self)
                divider.setFrameShape(QFrame.Shape.VLine)
                divider.setFrameShadow(QFrame.Shadow.Sunken)
                divider.setFixedHeight(14)
                self._divider_labels.append(divider)
                self._layout.addWidget(divider)
            else:
                self._divider_labels.append(None)
            self._layout.addWidget(label)

    def _tick(self) -> None:
        """One-second tick — update every visible clock label.

        Skips the rebuild entirely if the widget is hidden so we
        don't waste cycles formatting strings the user can't see.
        A misbehaving timezone (``ZoneInfoNotFoundError`` after the
        user manually edits the INI file) surfaces as ``"—:—:—"``
        rather than crashing the timer.
        """
        if not self.isVisible():
            return
        for clock, label in zip(self._collection.clocks, self._clock_labels):
            try:
                tz = ZoneInfo(clock.timezone)
                now = datetime.now(tz)
            except ZoneInfoNotFoundError:
                label.setText(f"{clock.timezone}: —:—:—")
                continue
            except Exception:  # noqa: BLE001 — defensive; user-edited data
                label.setText(f"{clock.timezone}: —:—:—")
                continue
            fmt = "%H:%M:%S" if clock.use_24h else "%I:%M:%S %p"
            text = now.strftime(fmt)
            # ``%Z`` adds the zone abbreviation (e.g. "EST") so the
            # user can tell identical times apart at a glance. Drop
            # it only if it's the same as the timezone string (avoids
            # ``America/New_York  America/New_York``).
            abbrev = now.tzname() or ""
            if abbrev and abbrev != clock.timezone:
                text = f"{text} {abbrev}"
            label.setText(text)