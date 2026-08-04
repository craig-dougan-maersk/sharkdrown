"""GlobalSettingsDialog — Claude > Settings.

Holds settings that are truly global to the user's Claude Code
installation (no longer per-profile):

- **General** — proxy bind / port / config_dir + default session CWD.
  Every profile routes through the same proxy.
- **Providers** — named ``Provider`` registry shared by every profile.
  Editing a provider's URL / API key / catalog updates every endpoint
  on every profile that names it.
- **API Keys** — the user's global ``CredentialStore``. (Already
  global at the storage layer; this tab moves the editor out of the
  per-profile dialog so the user sees it as a single global list.)
- **CLAUDE.md** — editor for ``~/.claude/CLAUDE.md``, the global rules
  document prepended into every profile's scoped ``CLAUDE.md``.

Construction pattern mirrors :class:`ProfileEditorDialog`: per-tab
builders live in ``_*.py`` mixins composed onto the dialog. The dialog
itself owns the menu wiring, atomic save, and accept handler.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.profiles.global_settings import GlobalSettings, GlobalSettingsStore

from src.ui.claude.global_settings._claude_md_tab import ClaudeMdTabMixin
from src.ui.claude.global_settings._api_keys_tab import ApiKeysTabMixin
from src.ui.claude.global_settings._general_tab import GeneralTabMixin
from src.ui.claude.global_settings._providers_tab import ProvidersTabMixin


class GlobalSettingsDialog(
    GeneralTabMixin,
    ProvidersTabMixin,
    ApiKeysTabMixin,
    ClaudeMdTabMixin,
    QDialog,
):
    """Edit the user's global Claude Code settings.

    Parameters
    ----------
    store:
        The :class:`GlobalSettingsStore` this dialog reads from and
        writes back to. Caller owns the store; the dialog mutates a
        copy and commits on OK.
    parent:
        Qt parent widget.
    """

    def __init__(
        self,
        store: GlobalSettingsStore,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Claude Settings")
        self.resize(720, 720)
        self._store = store
        self._settings = store.load()
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Settings shared across every Claude profile. Changes here apply "
            "to all profiles; per-profile settings live on the profile editor."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        self._build_general_tab()
        self._build_providers_tab()
        self._build_api_key_tab()
        self._build_claude_md_tab()

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _populate(self) -> None:
        self._populate_general_tab()
        self._populate_providers_tab()
        self._populate_keys_table()
        self._populate_claude_md_tab()

    def _on_accept(self) -> None:
        """Persist the edited settings to the backing store."""
        self._collect_general_tab()
        self._collect_providers_tab()
        self._collect_claude_md_tab()
        self._store.save(self._settings)
        self.accept()

    @property
    def settings(self) -> GlobalSettings:
        """Edited settings (caller reads on accept)."""
        return self._settings