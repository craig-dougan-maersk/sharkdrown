"""Embedded terminal sessions (PTY + pyte) for sharkdrownv3.

Public API:
- ``PtySession`` (``pty_backend``) — QThread wrapping a real PTY + child shell.
- ``screen_to_html`` (``renderer``) — pure pyte Screen → HTML string.
- ``TerminalSessionView`` (``terminal_session_view``) — one terminal session as
  a Qt widget.
- ``TerminalContainerView`` (``terminal_container_view``) — the page inserted
  into ``view_stack`` (post-refactor) that hosts multiple closable sessions.

Submodules are imported lazily to keep ``from src.ui.terminal import
screen_to_html`` working even before the PTY/Qt widgets are constructed
(useful for renderer-only tests).
"""

from src.ui.terminal.renderer import screen_to_html  # noqa: F401

__all__ = ["screen_to_html"]


def __getattr__(name):
    """Lazy-import the Qt-coupled pieces so renderer-only tests don't pull
    in PySide6 just to import this package."""
    if name == "PtySession":
        from src.ui.terminal.pty_backend import PtySession

        return PtySession
    if name == "TerminalSessionView":
        from src.ui.terminal.terminal_session_view import TerminalSessionView

        return TerminalSessionView
    if name == "TerminalContainerView":
        from src.ui.terminal.terminal_container_view import TerminalContainerView

        return TerminalContainerView
    raise AttributeError(f"module 'src.ui.terminal' has no attribute {name!r}")
