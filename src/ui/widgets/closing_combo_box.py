"""``ClosingComboBox`` — a ``QComboBox`` whose popup always closes cleanly.

The bug it fixes
----------------
``currentIndexChanged`` / ``currentTextChanged`` fire while Qt is still
processing the popup selection. When the connected slot does heavy
work — rebuilding another combo, re-rendering a preview, swapping a
stylesheet, repopulating a list — Qt's normal close-after-click
handling gets delayed and the popup remains visible. The user picks
an item, sees "the selected option only" stuck in the popup, and the
popup never dismisses.

The fix
-------
Connect to ``activated`` (the user-only signal that fires ONLY when
the user picks an item, NOT on programmatic ``setCurrentText`` /
``setCurrentIndex``) and ensure ``hidePopup()`` runs *before* any
downstream slot. ``format_combo`` at
``src/ui/main_window/_menu_construction.py:293-373`` has been doing
this manually since the bug was first observed; ``ClosingComboBox``
encapsulates the pattern so every combo in the app gets the fix for
free by subclassing and connecting to ``activated``.

Usage
-----
Replace ``QComboBox()`` with ``ClosingComboBox()`` and connect to
``activated`` instead of ``currentIndexChanged`` /
``currentTextChanged``. Programmatic selection
(``setCurrentText`` / ``setCurrentIndex``) bypasses ``activated``
entirely and is unaffected — there's no popup to close when state
changes programmatically.

Example::

    self.theme_combo = ClosingComboBox()
    self.theme_combo.addItems(["dark", "light", "auto"])
    # ``activated`` fires only on user pick; the subclass's
    # ``hidePopup`` runs first (because we connect at __init__ time,
    # before any user slot), so downstream work (stylesheet rebuild,
    # preview re-render) cannot leave the popup stuck open.
    self.theme_combo.activated.connect(self._on_theme_picked)
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox


class ClosingComboBox(QComboBox):
    """``QComboBox`` whose ``activated`` signal hides the popup first.

    Connect user-driven work to ``activated`` (the parent class's
    signal); this subclass installs a private handler at construction
    time that calls ``hidePopup()`` *before* any user-connected slot
    runs. Programmatic ``setCurrentText`` / ``setCurrentIndex`` paths
    bypass ``activated`` entirely and are unaffected.

    How it works
    ------------
    Qt's signal/slot dispatcher invokes connected slots in connection
    order. We connect ``_close_popup_first`` to ``super().activated``
    at construction time; user slots are typically connected *after*
    construction (via ``combo.activated.connect(my_slot)``), so Qt
    invokes our handler first. ``hidePopup()`` is safe to call
    unconditionally — Qt treats it as a no-op when the popup isn't
    visible — so a second invocation triggered by, say,
    ``setCurrentText`` inside a downstream slot is harmless.

    Why a subclass rather than an event filter / wrapper
    ----------------------------------------------------
    We could install an event filter on each ``QComboBox`` and
    intercept the popup's mouse-press event, but that fights Qt's
    internal popup lifecycle and breaks for editable combos where
    the line-edit has its own focus chain. A subclass that wires
    ``super().activated`` to a private ``hidePopup`` handler is the
    smallest, most-idiomatic intervention: subclasses inherit
    everything else from ``QComboBox`` (data binding, models,
    completion) and only override the one signal that fires during
    the buggy window.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # ``activated`` fires only on user pick (click / keyboard
        # selection), not on programmatic state changes. We connect
        # to ``super().activated`` here at construction time, so Qt
        # invokes our handler BEFORE any user slot connected later
        # via ``combo.activated.connect(my_slot)``.
        super().activated.connect(self._close_popup_first)

    def _close_popup_first(self, index: int) -> None:
        # ``hidePopup`` is a no-op when the popup isn't visible
        # (e.g. the user pressed Enter or the popup already
        # closed), so calling it unconditionally is safe.
        self.hidePopup()