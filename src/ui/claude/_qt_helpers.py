"""Shared Qt helpers used by both the profile editor and global settings dialog.

The profile editor's :class:`ProfileEditorDialog` grew a small ``_make_tab`` /
``_wrap_in_scroll`` pair of helpers; the new :class:`GlobalSettingsDialog`
needs the same wrappers. Rather than copy-paste the pattern, this module
holds the canonical implementations.

Each helper is a plain function — the dialog mixins that use them own the
``self`` they operate on. ``_make_tab`` wraps a child widget in a container
with consistent 8 px margins; ``_wrap_in_scroll`` wraps a widget in a
``QScrollArea`` with a configurable maximum height, hiding the frame so the
table blends into the surrounding dialog.
"""

from __future__ import annotations

from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget


def make_tab(inner: QWidget) -> QWidget:
    """Wrap ``inner`` in a container with the standard 8 px margins.

    Used by every per-tab builder — the dialog's ``QTabWidget`` owns the
    tab chrome, but each tab page wants its own padded container so a
    ``QFormLayout`` / ``QTableWidget`` doesn't bleed into the dialog frame.
    """
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.addWidget(inner)
    return container


def wrap_in_scroll(widget: QWidget, *, max_height: int) -> QScrollArea:
    """Wrap ``widget`` in a scroll area with a height cap.

    Tables and tree views that can grow unboundedly (model endpoints,
    environment variables, the plugins discovery tree) need a fixed
    vertical cap so a profile with dozens of rows doesn't push the
    rest of the dialog off-screen. ``setWidgetResizable(True)`` is
    required so the inner widget is sized to the scroll area's
    width; ``setMaximumHeight`` is what limits the vertical real
    estate before the scrollbar appears.

    The frame is hidden (``NoFrame``) so the table blends into the
    surrounding dialog — without this, a 1 px grey border makes the
    scroll region look like a separate panel.
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(widget)
    scroll.setMaximumHeight(max_height)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    return scroll