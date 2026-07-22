"""Claude Code session UI.

- :class:`ClaudeContainerView` — top-level tab widget that hosts a
  closable strip of Claude Code PTY sessions (mirrors
  :class:`src.ui.terminal.terminal_container_view.TerminalContainerView`).
- :class:`ProfileEditorDialog` — modal CRUD dialog for profiles.
"""

from src.ui.claude.claude_container_view import ClaudeContainerView
from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

__all__ = ["ClaudeContainerView", "ProfileEditorDialog"]