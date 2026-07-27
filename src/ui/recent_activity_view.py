"""Recent Activity tab — native Qt table view of captured proxy requests.

Why a QTableWidget and not another ``QWebEngineView``?
    The dashboard's HTML+htmx+Tailwind stack is great for a full
    modal with rendered conversation trees, but the Recent Activity
    tab is a flat *what just happened* surface. A native table:

    - Renders instantly (no second QWebEngine process).
    - Doesn't pull Tailwind / Alpine.js / htmx into this code path.
    - Plays nicely with high-frequency polling (every 5s) since
      repopulating cells is cheaper than swapping HTML.

Filtering
---------
The two top-of-pane checkboxes (``Hide failed``, ``Hide 0-token``)
mirror the dashboard's toggle semantics: both default ON, both are
round-tripped to the server via the ``hide_*`` query params so the
URL is fully self-describing and bookmarkable.

Polling
-------
A ``QTimer`` fires ``_refresh`` every 5 seconds. While the request
is in-flight the table is *not* blanked — we replace the model atomically
once the response arrives. Failures are surfaced in the status label
below the table (``— offline``) but don't throw.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("sharkdrown.recent_activity")

_REFRESH_MS = 5000
_HTTP_TIMEOUT_S = 3.0

# Column indices — keep in sync with the header labels and _populate_row().
_COL_TIME = 0
_COL_MODEL = 1
_COL_PATH = 2
_COL_STATUS = 3
_COL_TOKENS = 4
_COL_COUNT = 5


class RecentActivityView(QWidget):
    """QWidget that polls /api/requests and renders rows in a QTableWidget.

    Signals
    -------
    request_activated(str)
        Emitted with the request id when the user double-clicks a row.
        Listeners may open the dashboard detail modal via the proxy URL.
    """

    request_activated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._base_url: str = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ---- Filter row -----------------------------------------------
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        title = QLabel("Recent Activity")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(12)
        title.setFont(title_font)
        filter_row.addWidget(title)

        filter_row.addSpacing(16)
        filter_row.addWidget(QLabel("Filter:"))

        # ``Hide failed`` and ``Hide 0-token`` default ON to match the
        # dashboard toggle. Toggling triggers an immediate refresh.
        self.hide_failed_chk = QCheckBox("Hide failed")
        self.hide_failed_chk.setChecked(True)
        self.hide_failed_chk.stateChanged.connect(self._schedule_refresh)
        filter_row.addWidget(self.hide_failed_chk)

        self.hide_no_tokens_chk = QCheckBox("Hide 0-token")
        self.hide_no_tokens_chk.setChecked(True)
        self.hide_no_tokens_chk.stateChanged.connect(self._schedule_refresh)
        filter_row.addWidget(self.hide_no_tokens_chk)

        filter_row.addStretch(1)
        outer.addLayout(filter_row)

        # ---- Table -----------------------------------------------------
        self.table = QTableWidget()
        self.table.setColumnCount(_COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Model", "Path", "Status", "Tokens"]
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(_COL_TIME, 110)
        self.table.setColumnWidth(_COL_MODEL, 220)
        self.table.setColumnWidth(_COL_PATH, 110)
        self.table.setColumnWidth(_COL_STATUS, 90)
        # Tokens column auto-stretches.
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(_COL_TOKENS, QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(False)
        self.table.cellDoubleClicked.connect(self._on_row_activated)
        outer.addWidget(self.table, 1)

        # ---- Status label ---------------------------------------------
        self.status_label = QLabel("Waiting for proxy…")
        self.status_label.setStyleSheet("color: gray; font-size: 11px;")
        outer.addWidget(self.status_label)

        # ---- Refresh timer --------------------------------------------
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._pending_refresh: QTimer | None = None
        self._client = httpx.Client(timeout=_HTTP_TIMEOUT_S)

    # -- Public API ------------------------------------------------------
    def set_base_url(self, url: str) -> None:
        """Point the view at a freshly-started proxy URL.

        Resets the table so a stale list from a previous run is gone
        before the first response lands.
        """
        self._base_url = url.rstrip("/")
        self.table.setRowCount(0)
        if self._base_url:
            self._timer.start()
            self._refresh()
            self.status_label.setText("Loading…")
        else:
            self._timer.stop()
            self.status_label.setText("Waiting for proxy…")

    def stop(self) -> None:
        """Stop polling and clear the table."""
        self._timer.stop()
        self._base_url = ""
        self.table.setRowCount(0)
        self.status_label.setText("Proxy stopped.")

    def shutdown(self) -> None:
        """Tear down owned resources. Safe to call multiple times."""
        self.stop()
        try:
            self._client.close()
        except OSError:
            pass

    # -- Slots -----------------------------------------------------------
    def _schedule_refresh(self, *_args: Any) -> None:
        """Coalesce state-change→refresh storms into a single fetch.

        A user toggling both checkboxes in quick succession would
        otherwise fire two HTTP requests back-to-back. The debounce
        here waits 200ms of idle before issuing the next fetch.
        """
        if self._pending_refresh is not None:
            self._pending_refresh.stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(200)
        timer.timeout.connect(self._refresh)
        timer.start()
        self._pending_refresh = timer

    def _refresh(self) -> None:
        """Fetch /api/requests and replace the table contents."""
        if not self._base_url:
            return
        params: list[tuple[str, str]] = [("limit", "50"), ("page", "1")]
        params.append(
            ("hide_failed", "1" if self.hide_failed_chk.isChecked() else "0")
        )
        params.append(
            (
                "hide_no_tokens",
                "1" if self.hide_no_tokens_chk.isChecked() else "0",
            )
        )
        # httpx takes a list of tuples so order is preserved in the URL.
        from urllib.parse import urlencode

        url = f"{self._base_url}/api/requests?{urlencode(params)}"
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            logger.debug("recent-activity refresh failed: %s", exc)
            self.status_label.setText(f"— offline ({type(exc).__name__})")
            return
        if resp.status_code != 200:
            self.status_label.setText(
                f"— proxy returned HTTP {resp.status_code}"
            )
            return
        try:
            payload = resp.json()
        except ValueError as exc:
            self.status_label.setText(f"— invalid JSON: {exc}")
            return

        rows = payload.get("requests", [])
        self._populate_table(rows)
        total = payload.get("total", len(rows))
        now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
        self.status_label.setText(
            f"{len(rows)} of {total} requests · last refresh {now}"
        )

    def _on_row_activated(self, row: int, _col: int) -> None:
        """Open the request's detail page in the user's browser."""
        item = self.table.item(row, _COL_TIME)
        if item is None:
            return
        request_id = item.data(Qt.ItemDataRole.UserRole)
        if not request_id:
            return
        # Broadcast first so other listeners (e.g. main window) can react.
        self.request_activated.emit(request_id)
        # Default behaviour: open the dashboard modal route directly.
        QDesktopServices.openUrl(
            QUrl(f"{self._base_url}/partials/request/{request_id}")
        )

    # -- Internals -------------------------------------------------------
    def _populate_table(self, rows: list[dict[str, Any]]) -> None:
        """Replace the table's row set with ``rows`` (atomic-ish)."""
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            self._populate_row(idx, row)

    def _populate_row(self, idx: int, row: dict[str, Any]) -> None:
        """Fill one row's cells; reads ``row`` defensively."""
        request_id = str(row.get("id") or "")
        ts = _format_time(row.get("timestamp"))
        model = str(row.get("original_model") or row.get("routed_model") or "—")
        path = str(row.get("request_path") or "—")
        status = row.get("status_code")
        tokens_in = row.get("input_tokens")
        tokens_out = row.get("output_tokens")
        tokens_total = row.get("total_tokens")
        token_text = _format_tokens(tokens_in, tokens_out, tokens_total)

        # Time cell carries the request id so double-click can find it.
        time_item = QTableWidgetItem(ts)
        time_item.setData(Qt.ItemDataRole.UserRole, request_id)
        self.table.setItem(idx, _COL_TIME, time_item)

        self.table.setItem(idx, _COL_MODEL, QTableWidgetItem(model))
        self.table.setItem(idx, _COL_PATH, QTableWidgetItem(path))
        status_item = QTableWidgetItem(_format_status(status))
        _color_status(status_item, status)
        self.table.setItem(idx, _COL_STATUS, status_item)
        token_item = QTableWidgetItem(token_text)
        _color_tokens(token_item, tokens_total)
        self.table.setItem(idx, _COL_TOKENS, token_item)


# ---- Helpers ----------------------------------------------------------------


def _format_time(value: Any) -> str:
    """Render an ISO timestamp as HH:MM:SS for compactness."""
    if not value:
        return "—"
    try:
        # ISO strings are 'YYYY-MM-DDTHH:MM:SS[.ffffff][+TZ]' — strip the date.
        return str(value).split("T", 1)[-1][:8]
    except (ValueError, IndexError, AttributeError):
        return str(value)


def _format_status(value: Any) -> str:
    """Render a status code as a short label."""
    if value is None:
        return "—"
    return str(int(value))


def _color_status(item: QTableWidgetItem, status: Any) -> None:
    """Tint the status cell by HTTP class (2xx green, 4xx amber, 5xx red)."""
    if status is None:
        item.setForeground(Qt.GlobalColor.gray)
        return
    try:
        code = int(status)
    except (TypeError, ValueError):
        return
    if 500 <= code:
        item.setForeground(Qt.GlobalColor.red)
    elif code >= 400:
        item.setForeground(Qt.GlobalColor.darkYellow)
    elif code >= 200:
        item.setForeground(Qt.GlobalColor.darkGreen)


def _format_tokens(
    input_tokens: Any, output_tokens: Any, total_tokens: Any
) -> str:
    """Render a one-line token summary; ``None`` renders as dashes."""
    if total_tokens is None:
        return "—"
    try:
        total = int(total_tokens)
    except (TypeError, ValueError):
        return "—"
    parts = [f"{total:,} tok"]
    if input_tokens is not None and output_tokens is not None:
        try:
            parts.append(f"({int(input_tokens):,}↓/{int(output_tokens):,}↑)")
        except (TypeError, ValueError):
            pass
    return " ".join(parts)


def _color_tokens(item: QTableWidgetItem, total_tokens: Any) -> None:
    """Tint the token cell: green when populated, gray when missing."""
    if total_tokens is None or total_tokens == 0:
        item.setForeground(Qt.GlobalColor.gray)
        return
    item.setForeground(Qt.GlobalColor.darkGreen)
