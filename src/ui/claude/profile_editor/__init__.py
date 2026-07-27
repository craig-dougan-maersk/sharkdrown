"""Profile editor dialog — refactored into per-tab widget modules.

Public API:
    ProfileEditorDialog — modal editor for a single ClaudeProfile
    ManageProfilesDialog — list-and-act dialog for profile collection
    StandardEnvVarsDialog — picker for curated env vars
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
