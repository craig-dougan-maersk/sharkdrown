"""Data model + persistence for the world-clock feature.

Each :class:`Clock` is one row in the manager dialog and one
``QLabel`` in the status bar. :class:`ClockCollection` is the
persisted shape (master ``enabled`` toggle + ordered list of clocks).

Storage uses ``QSettings("SharkDrown", "App")`` — the same convention
used everywhere else in this UI (``_menu_construction.py``,
``settings_dialog.py``, ``proxy_config.py``). Two keys:

- ``clocks_enabled`` — bool, master toggle.
- ``clocks_data``    — JSON string ``[{id, timezone, use_24h}, ...]``.

JSON-as-string avoids ``QSettings``' nested-array semantics, which
are flaky across platforms and Qt versions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QSettings


# QSettings keys. Top-level (no group) — keeps the file diffable in
# the INI file the user might inspect for debugging. Mirrors the
# pattern in :mod:`src.ui.terminal.settings_dialog` and
# :mod:`src.proxy.proxy_config`.
_KEY_ENABLED = "clocks_enabled"
_KEY_DATA = "clocks_data"


@dataclass(frozen=True)
class Clock:
    """One world-clock row.

    Attributes
    ----------
    id:
        Stable identifier (auto-incremented by the dialog). Preserved
        across edits so the dialog can match rows back to their
        underlying record even after a reorder.
    timezone:
        IANA name (``zoneinfo.ZoneInfo``-compatible), e.g.
        ``"America/Los_Angeles"``. We don't validate this at the
        dataclass level — the dialog validates against
        :func:`zoneinfo.available_timezones` before constructing.
    use_24h:
        ``True`` → 24-hour (``%H:%M:%S``); ``False`` → 12-hour
        (``%I:%M:%S %p``).
    """

    id: int
    timezone: str
    use_24h: bool

    def as_dict(self) -> dict:
        """Render as a JSON-serialisable dict."""
        return {"id": self.id, "timezone": self.timezone, "use_24h": self.use_24h}


@dataclass
class ClockCollection:
    """The persisted shape — master toggle plus ordered clocks."""

    enabled: bool = False
    clocks: list[Clock] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Defensive default — the dataclass pattern uses mutable defaults
        # through ``None`` + ``__post_init__`` so callers don't share a
        # single list across instances.
        if self.clocks is None:
            self.clocks = []


def _settings() -> QSettings:
    """Return the singleton ``QSettings`` used for clock persistence.

    The two-arg constructor (``org``, ``app``) is the dominant
    pattern in this codebase — see :mod:`src.ui.terminal.settings_dialog`
    and :mod:`src.ui.menu_construction`. ``QSettings()`` defaults to
    the app's registered org/app, but explicit is safer for tests
    that monkeypatch the INI path.
    """
    return QSettings("SharkDrown", "App")


def load_clocks() -> ClockCollection:
    """Read the current :class:`ClockCollection` from ``QSettings``.

    Defaults to a disabled, empty collection when nothing is stored —
    a fresh install shouldn't show clocks out of the box.
    """
    s = _settings()
    enabled = s.value(_KEY_ENABLED, False, type=bool)
    raw = s.value(_KEY_DATA, "[]", type=str)
    clocks: list[Clock] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                try:
                    clocks.append(
                        Clock(
                            id=int(entry.get("id") or 0),
                            timezone=str(entry.get("timezone") or "").strip(),
                            use_24h=bool(entry.get("use_24h", True)),
                        )
                    )
                except (TypeError, ValueError):
                    continue
    return ClockCollection(enabled=enabled, clocks=clocks)


def save_clocks(coll: ClockCollection) -> None:
    """Persist the collection. Drops entries with empty timezones."""
    s = _settings()
    s.setValue(_KEY_ENABLED, bool(coll.enabled))
    cleaned = [c.as_dict() for c in coll.clocks if c.timezone]
    s.setValue(_KEY_DATA, json.dumps(cleaned))


def next_clock_id(existing: list[Clock]) -> int:
    """Return a fresh id one above the highest current id.

    Used by the dialog when adding a new clock so ids never collide
    with existing rows even after deletes (we don't recycle ids to
    avoid surprising the user if they later add a new one and see a
    "different" id than they expected).
    """
    if not existing:
        return 1
    return max(c.id for c in existing) + 1