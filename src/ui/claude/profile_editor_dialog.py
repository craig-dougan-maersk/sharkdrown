"""Backwards-compatibility shim — the real implementation lives in
``src.ui.claude.profile_editor`` (the package).

All public names are re-exported here so existing imports like::

    from src.ui.claude.profile_editor_dialog import ProfileEditorDialog
    from src.ui.claude.profile_editor_dialog import ManageProfilesDialog

continue to work without modification.
"""

from src.ui.claude.profile_editor.dialog import ProfileEditorDialog
from src.ui.claude.profile_editor._dialogs import (
    ManageProfilesDialog,
    StandardEnvVarsDialog,
)

__all__ = [
    "ManageProfilesDialog",
    "ProfileEditorDialog",
    "StandardEnvVarsDialog",
]
