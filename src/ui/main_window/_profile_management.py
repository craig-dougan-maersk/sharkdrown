"""Profile management mixin — Claude sessions, profile switching, editors."""

from __future__ import annotations

import copy

from PySide6.QtWidgets import QInputDialog, QMessageBox

from src.profiles.store import ProfileStore
from src.proxy.paths import default_db_path


class ProfileManagementMixin:
    """Manages Claude profiles and session creation."""

    def _profile_store(self) -> ProfileStore:
        """Lazy-init the :class:`ProfileStore` (requires a QApplication)."""
        if self.profile_store is None:
            self.profile_store = ProfileStore(
                path=default_db_path().parent / "profiles.json",
            )
        return self.profile_store

    def _on_claude_proxy_requested(self) -> None:
        """Slot for :attr:`ClaudeContainerView.proxy_requested`."""
        self._request_proxy_start()

    def _new_claude_session(self) -> None:
        """Open a new Claude session in the Claude tab and switch to it."""
        if self.claude_container is None:
            return
        profiles = self._profile_store().load()
        if not profiles:
            QMessageBox.information(
                self,
                "No profiles",
                "No Claude profiles configured. Open "
                "Claude -> Manage Profiles... to create one.",
            )
            return
        active = self._profile_store().get_active()
        profile = active
        if len(profiles) > 1:
            names = [p.name for p in profiles]
            current = names.index(active.name) if active else 0
            choice, ok = QInputDialog.getItem(
                self,
                "Profile for new session",
                "Use which profile?",
                names,
                current,
                False,
            )
            if not ok:
                return
            profile = next(p for p in profiles if p.name == choice)
        self.claude_container.new_session(profile=profile)
        self.view_tabs.setCurrentIndex(self._TAB_INDEX_CLAUDE)

    def _focus_claude_tab(self) -> None:
        """Slot for Claude -> Focus Claude Tab."""
        idx = getattr(self, "_TAB_INDEX_CLAUDE", None)
        if idx is not None:
            self.view_tabs.setCurrentIndex(idx)

    def _set_active_profile(self, name: str) -> None:
        """Switch the active profile; restart the proxy if running."""
        store = self._profile_store()
        try:
            store.set_active(name)
        except KeyError:
            return
        if self._is_proxy_running():
            self.status.showMessage(
                f"Restarting proxy to apply profile {name!r}…",
                3000,
            )
            self._restart_after_stop = True
            self._stop_server()
        else:
            self._refresh_profile_menu()

    def _show_profile_editor(self) -> None:
        """Open the Manage Profiles list dialog."""
        from src.ui.claude.profile_editor_dialog import ManageProfilesDialog

        store = self._profile_store()
        dlg = ManageProfilesDialog(store, parent=self)
        prev_active = store.get_active_name()
        was_running = self._is_proxy_running()
        dlg.exec()
        self._refresh_profile_menu()
        new_active = store.get_active_name()
        if was_running and new_active == prev_active:
            self.status.showMessage(
                "Restarting proxy to apply profile changes…", 3000
            )
            self._restart_after_stop = True
            self._stop_server()

    def _show_proxy_settings(self) -> None:
        """Open the active profile's editor."""
        from src.ui.claude.profile_editor_dialog import ProfileEditorDialog

        store = self._profile_store()
        active = store.get_active()
        if active is None:
            active = store.ensure_default()
        was_running = self._is_proxy_running()
        dlg = ProfileEditorDialog(
            copy.deepcopy(active),
            credential_store=store.credential_store,
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        edited = dlg.to_profile()
        if not edited.name:
            return
        store.upsert(edited)
        self.status.showMessage(f"Saved profile {edited.name!r}.", 3000)
        self._refresh_profile_menu()
        if was_running:
            self.status.showMessage(
                "Restarting proxy to apply profile changes…", 3000
            )
            self._restart_after_stop = True
            self._stop_server()

    def _refresh_profile_menu(self) -> None:
        """Rebuild the profile radio list from the on-disk store."""
        from PySide6.QtGui import QAction

        for action in list(self.profile_group.actions()):
            self.profile_group.removeAction(action)
            self.profile_menu.removeAction(action)

        store = self._profile_store()
        profiles = store.load()
        active_name = store.get_active_name() or (
            profiles[0].name if profiles else None
        )

        for p in profiles:
            label = p.name
            if getattr(p, "is_default", False) or p.name == "Default":
                label = f"{p.name}  (default)"
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(p.name == active_name)
            act.triggered.connect(lambda _checked=False, n=p.name: self._set_active_profile(n))
            self.profile_group.addAction(act)
            self.profile_menu.addAction(act)

        if not profiles:
            placeholder = QAction("(no profiles)", self)
            placeholder.setEnabled(False)
            self.profile_menu.addAction(placeholder)
