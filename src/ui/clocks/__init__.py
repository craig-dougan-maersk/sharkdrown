"""World-clock feature — bottom-left status-bar widget + manager dialog.

Public surface
--------------
- :class:`Clock` — one row of the manager (id, timezone, 12h/24h)
- :class:`ClockCollection` — the persisted shape (master toggle + clocks)
- :func:`load_clocks` / :func:`save_clocks` — ``QSettings`` round-trip
- :func:`next_clock_id` — fresh id one above the current maximum
- :class:`ClocksDialog` — modal manager reached from View → Clocks…
- :class:`ClockEditorDialog` — small editor for one row (used by
  Add… and Edit…; re-exported so tests can drive ``exec()``)
- :class:`ClockToolbarWidget` — bottom-left status-bar renderer
"""

from src.ui.clocks.clocks_dialog import ClockEditorDialog, ClocksDialog
from src.ui.clocks.models import (
    Clock,
    ClockCollection,
    load_clocks,
    next_clock_id,
    save_clocks,
)
from src.ui.clocks.toolbar_widget import ClockToolbarWidget


__all__ = [
    "Clock",
    "ClockCollection",
    "ClockEditorDialog",
    "ClockToolbarWidget",
    "ClocksDialog",
    "load_clocks",
    "next_clock_id",
    "save_clocks",
]