"""Persistent status-bar indicator showing live proxy activity.

The tile is a small ``QLabel`` placed to the right of the existing
``proxy_status_label`` in the main window's status bar. It polls
``GET /api/stats`` every 3 seconds and renders ``N req / M err / K
empty`` — a glanceable summary that doesn't require opening the
proxy dashboard.

Filtering semantics
--------------------
The three numbers are presented **without** the dashboard's
hide-failed / hide-no-tokens toggles applied, because the user wants
to know *whether* there's anything hidden, not *what* remains after
filtering. Failed and empty rows still count toward the total; the
indicator surfaces both buckets because they're the kinds of requests
worth investigating.

Visual cues
-----------
- Healthy state (no errors / no empty): ``#2563eb`` blue text.
- With errors: ``#dc2626`` red text via inline stylesheet (no
  theme/color coupling).
- Empty but no errors: gray text.
- Offline / fetch error: italic ``— proxy offline``.

Inputs
------
``set_base_url(url)`` — call from the main window when the embedded
proxy signals it's listening, and clear it again on stop. The tile
emits ``clicked`` when the user clicks it (mouse press), letting the
main window switch to the relevant tab.
"""

from __future__ import annotations

import logging

import httpx
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel

logger = logging.getLogger("sharkdrown.activity_tile")

_REFRESH_MS = 3000          # poll cadence
_HTTP_TIMEOUT_S = 2.0       # short — a slow request shouldn't block the UI
_OFFLINE_TEXT = "— proxy offline"
_IDLE_TEXT = "… waiting for proxy"

_GREEN_STYLE = "color: #166534; padding: 2px 6px;"      # healthy
_RED_STYLE = "color: #b91c1c; padding: 2px 6px;"        # errors present
_AMBER_STYLE = "color: #b45309; padding: 2px 6px;"      # empty rows but no errors
_GRAY_STYLE = "color: #6b7280; padding: 2px 6px; font-style: italic;"


class ActivityTile(QLabel):
    """Poll-and-display widget for embedded proxy stats.

    Signals
    -------
    clicked()
        Emitted when the user clicks the tile (mouse press, left button).
        The main window connects this to ``switch to dashboard tab``.
    """

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(_IDLE_TEXT, parent)
        self._base_url: str = ""
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self.setToolTip(
            "Live proxy activity. Click to open the dashboard.\n"
            "Counts include all captured requests (errors and empty rows visible)."
        )
        self.setStyleSheet(_GRAY_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- Public API ------------------------------------------------------
    def set_base_url(self, url: str) -> None:
        """Point the tile at a freshly-started proxy.

        Immediately refreshes so the first paint doesn't show idle dots.
        """
        self._base_url = url.rstrip("/")
        if self._base_url:
            self._timer.start()
            self._refresh()
        else:
            self._timer.stop()
            self.setText(_IDLE_TEXT)
            self.setStyleSheet(_GRAY_STYLE)

    def stop(self) -> None:
        """Stop polling (called from the main window when the proxy stops)."""
        self._timer.stop()
        self._base_url = ""
        self.setText(_IDLE_TEXT)
        self.setStyleSheet(_GRAY_STYLE)

    # -- Slots -----------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        # Treat any left-button click as an activation; ignore other buttons.
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    # -- Internals -------------------------------------------------------
    def _refresh(self) -> None:
        """Fetch /api/stats and update the label."""
        if not self._base_url:
            self.setText(_IDLE_TEXT)
            self.setStyleSheet(_GRAY_STYLE)
            return
        url = f"{self._base_url}/api/stats"
        try:
            # Synchronous call from the Qt main thread; the 2s timeout
            # keeps the worst-case stall bounded. Use a fresh Client
            # per call to avoid stale-connection state — volume is tiny.
            with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                self._render_offline(f"http {resp.status_code}")
                return
            data = resp.json()
            self._render_counts(
                total=int(data.get("total", 0)),
                failed=int(data.get("failed", 0)),
                empty=int(data.get("empty", 0)),
                last_model=(data.get("last") or {}).get("model"),
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.debug("activity tile fetch failed: %s", exc)
            self._render_offline(type(exc).__name__)

    def _render_counts(
        self, *, total: int, failed: int, empty: int, last_model: str | None
    ) -> None:
        """Render the three-count pill and pick a color."""
        self.setText(f"{total} req / {failed} err / {empty} empty")
        if failed > 0:
            self.setStyleSheet(_RED_STYLE)
        elif empty > 0:
            self.setStyleSheet(_AMBER_STYLE)
        else:
            self.setStyleSheet(_GREEN_STYLE)
        tip_parts = [
            f"Captured: {total}",
            f"Failed (status ≥ 400): {failed}",
            f"Empty (no token data): {empty}",
        ]
        if last_model:
            tip_parts.append(f"Last: {last_model}")
        tip_parts.append("Click to open the dashboard.")
        self.setToolTip("\n".join(tip_parts))

    def _render_offline(self, reason: str) -> None:
        self.setText(f"{_OFFLINE_TEXT} ({reason})")
        self.setStyleSheet(_GRAY_STYLE)
