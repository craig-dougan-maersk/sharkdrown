"""Persistent status-bar indicator showing live proxy activity.

The tile is a small ``QLabel`` placed to the right of the existing
``proxy_status_label`` in the main window's status bar. It receives
push events from the EventBusBridge (request_created / response_complete)
and maintains running counters — no HTTP polling required.

An initial fetch of /api/stats seeds the counters with the current DB
state so restarts mid-session pick up where they left off.

Visual cues
-----------
- Healthy state (no errors / no empty): green text.
- With errors: red text.
- Empty but no errors: amber text.
- Offline / not started: gray italic.

The tile emits ``clicked`` when the user clicks it, letting the
main window switch to the relevant tab.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel

from ccproxy.app.pricing import compute_cost

logger = logging.getLogger("sharkdrown.activity_tile")

_HTTP_TIMEOUT_S = 2.0
_OFFLINE_TEXT = "— proxy offline"
_IDLE_TEXT = "… waiting for proxy"

_GREEN_STYLE = "color: #166534; padding: 2px 6px;"
_RED_STYLE = "color: #b91c1c; padding: 2px 6px;"
_AMBER_STYLE = "color: #b45309; padding: 2px 6px;"
_GRAY_STYLE = "color: #6b7280; padding: 2px 6px; font-style: italic;"


class ActivityTile(QLabel):
    """Event-driven widget for embedded proxy stats.

    Signals
    -------
    clicked()
        Emitted when the user clicks the tile (mouse press, left button).
    """

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(_IDLE_TEXT, parent)
        self._base_url: str = ""
        self._total: int = 0
        self._failed: int = 0
        self._empty: int = 0
        self._total_cost: float = 0.0
        self.setToolTip(
            "Live proxy activity. Click to open the dashboard.\n"
            "Counts include all captured requests (errors and empty rows visible)."
        )
        self.setStyleSheet(_GRAY_STYLE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -- Public API ------------------------------------------------------
    def set_base_url(self, url: str) -> None:
        """Point the tile at a freshly-started proxy.

        Fetches /api/stats once to seed counters, then relies on push events.
        """
        self._base_url = url.rstrip("/")
        if self._base_url:
            self._seed_from_stats()
        else:
            self._reset()

    def stop(self) -> None:
        """Reset to idle state (called when the proxy stops)."""
        self._reset()

    # -- Event slots (connected to EventBusBridge) -------------------------
    def on_request_created(self, event: dict[str, Any]) -> None:
        """Handle a new request being captured."""
        self._total += 1
        self._empty += 1  # assume empty until response arrives
        self._render()

    def on_response_complete(self, event: dict[str, Any]) -> None:
        """Handle a response completing for a previously-created request."""
        status_code = event.get("status_code")
        total_tokens = event.get("total_tokens")
        input_tokens = event.get("input_tokens") or 0
        output_tokens = event.get("output_tokens") or 0
        model = event.get("model") or ""

        # It was counted as empty on creation; correct if tokens arrived
        if total_tokens is not None and total_tokens > 0:
            self._empty = max(0, self._empty - 1)

        if status_code is not None and status_code >= 400:
            self._failed += 1

        # Accumulate cost
        if model and (input_tokens or output_tokens):
            cost = compute_cost(model, input_tokens, output_tokens)
            if cost is not None:
                self._total_cost += cost

        self._render()

    # -- Mouse interaction -------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    # -- Internals ---------------------------------------------------------
    def _seed_from_stats(self) -> None:
        """One-shot HTTP fetch to seed counters from the DB state."""
        url = f"{self._base_url}/api/stats"
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                self._total = int(data.get("total", 0))
                self._failed = int(data.get("failed", 0))
                self._empty = int(data.get("empty", 0))
                self._total_cost = float(data.get("total_cost_usd", 0.0))
                self._render()
                return
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.debug("activity tile seed failed: %s", exc)
        self._render_offline("seed failed")

    def _render(self) -> None:
        """Update the label text and color from current counters."""
        cost_str = f"${self._total_cost:.4f}" if self._total_cost > 0 else ""
        parts = [f"{self._total} req"]
        if self._failed:
            parts.append(f"{self._failed} err")
        if cost_str:
            parts.append(cost_str)
        self.setText(" / ".join(parts))
        if self._failed > 0:
            self.setStyleSheet(_RED_STYLE)
        elif self._empty > 0:
            self.setStyleSheet(_AMBER_STYLE)
        else:
            self.setStyleSheet(_GREEN_STYLE)
        tip_parts = [
            f"Captured: {self._total}",
            f"Failed (status >= 400): {self._failed}",
            f"Empty (no token data): {self._empty}",
            f"Session cost: ${self._total_cost:.4f}",
            "Click to open the dashboard.",
        ]
        self.setToolTip("\n".join(tip_parts))

    def _render_offline(self, reason: str) -> None:
        self.setText(f"{_OFFLINE_TEXT} ({reason})")
        self.setStyleSheet(_GRAY_STYLE)

    def _reset(self) -> None:
        self._base_url = ""
        self._total = 0
        self._failed = 0
        self._empty = 0
        self._total_cost = 0.0
        self.setText(_IDLE_TEXT)
        self.setStyleSheet(_GRAY_STYLE)
