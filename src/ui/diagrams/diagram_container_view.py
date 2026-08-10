"""Container page for the "Diagrams" top-level tab.

Hosts a closable ``QTabWidget`` of ``DiagramPane`` instances plus an
empty-state placeholder. This widget is the page inserted into
``view_stack`` as the 1st top-level page in
``src/ui/main_window/window.py``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .diagram_pane import DiagramPane


class DiagramContainerView(QWidget):
    """Page widget: closable tab strip of diagram panes + empty state."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._panes: list[DiagramPane] = []
        self._untitled_counter = 0
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Empty state --------------------------------------------------
        # Shown instead of the tab widget while no diagrams are open, so
        # the tab doesn't present a blank void on first visit.
        self.info_label = QLabel(
            "No diagrams open — use Diagrams → New Diagram (Ctrl+Alt+D)."
        )
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(self.info_label, 1)

        # --- Inner tab widget (closable, movable) -------------------------
        # Closable + movable mirrors ``editor_tabs`` and the terminal
        # container's ``session_tabs`` so the strip feels familiar.
        self.diagram_tabs = QTabWidget()
        self.diagram_tabs.setTabsClosable(True)
        self.diagram_tabs.setMovable(True)
        self.diagram_tabs.tabCloseRequested.connect(self._close_diagram)
        self.diagram_tabs.setVisible(False)
        layout.addWidget(self.diagram_tabs, 1)

        # --- Alt+Left / Alt+Right diagram cycling -------------------------
        # ``WidgetWithChildrenShortcut`` scopes these to this widget's
        # focus chain, matching TerminalContainerView — an application
        # scope would steal Alt+arrows from the Documents tab.
        self.action_prev_diagram = QAction("Previous Diagram", self)
        self.action_prev_diagram.setShortcut(QKeySequence("Alt+Left"))
        self.action_prev_diagram.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.action_prev_diagram.triggered.connect(lambda: self._cycle_diagram(-1))
        self.addAction(self.action_prev_diagram)

        self.action_next_diagram = QAction("Next Diagram", self)
        self.action_next_diagram.setShortcut(QKeySequence("Alt+Right"))
        self.action_next_diagram.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.action_next_diagram.triggered.connect(lambda: self._cycle_diagram(+1))
        self.addAction(self.action_next_diagram)

    # ---- public API --------------------------------------------------------

    def new_diagram(self, name: str | None = None) -> DiagramPane:
        """Open a new diagram pane as a tab and return it."""
        if name is None:
            self._untitled_counter += 1
            name = f"untitled-{self._untitled_counter}.mmd"

        pane = DiagramPane(name, parent=self)
        pane.document_modified.connect(self._on_pane_modified)
        index = self.diagram_tabs.addTab(pane, pane.title())
        self.diagram_tabs.setCurrentIndex(index)
        self._panes.append(pane)
        self._update_empty_state()
        return pane

    def open_diagram(self, path: Path) -> DiagramPane:
        """Open an existing .mmd file in a new diagram tab."""
        pane = DiagramPane(path.name, parent=self)
        pane.load(path)
        pane.document_modified.connect(self._on_pane_modified)
        index = self.diagram_tabs.addTab(pane, pane.title())
        self.diagram_tabs.setCurrentIndex(index)
        self._panes.append(pane)
        self._update_empty_state()
        return pane

    def focus_active_diagram(self) -> None:
        """Focus the currently-selected pane.

        Called by MainWindow when the Diagrams top-level tab becomes
        current — ``QTabWidget.currentChanged`` alone doesn't transfer
        focus from the QTabBar to the visible widget.
        """
        widget = self.diagram_tabs.currentWidget()
        if widget is not None:
            widget.setFocus()

    @property
    def diagram_count(self) -> int:
        """Number of currently-open diagram panes."""
        return len(self._panes)

    def shutdown(self) -> None:
        """Idempotent full-shutdown of every pane."""
        # Snapshot the list — shutdown() may mutate it via close signals.
        for pane in list(self._panes):
            try:
                pane.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._panes.clear()
        self.diagram_tabs.clear()
        self._update_empty_state()

    # ---- internals ---------------------------------------------------------

    def _cycle_diagram(self, delta: int) -> None:
        """Switch to the next/previous diagram, wrapping at the edges."""
        count = self.diagram_tabs.count()
        if count <= 1:
            return
        new_index = (self.diagram_tabs.currentIndex() + delta) % count
        self.diagram_tabs.setCurrentIndex(new_index)
        self.focus_active_diagram()

    def _close_diagram(self, index: int) -> None:
        """Slot for the inner tab widget's ``tabCloseRequested``."""
        widget = self.diagram_tabs.widget(index)
        self.diagram_tabs.removeTab(index)
        if isinstance(widget, DiagramPane):
            try:
                widget.shutdown()
            except Exception:  # noqa: BLE001
                pass
            if widget in self._panes:
                self._panes.remove(widget)
            widget.deleteLater()
        self._update_empty_state()

    def _on_pane_modified(self) -> None:
        """Update the tab title when a pane's dirty state changes."""
        for i in range(self.diagram_tabs.count()):
            pane = self.diagram_tabs.widget(i)
            if isinstance(pane, DiagramPane):
                self.diagram_tabs.setTabText(i, pane.title())

    def _update_empty_state(self) -> None:
        """Swap between the placeholder and the tab strip."""
        has_panes = self.diagram_tabs.count() > 0
        self.diagram_tabs.setVisible(has_panes)
        self.info_label.setVisible(not has_panes)
