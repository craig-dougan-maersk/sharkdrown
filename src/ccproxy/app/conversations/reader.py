"""JSONL reader for Claude Code's on-disk conversation history.

Claude Code persists every session to ``~/.claude/projects/<project>/<session-id>.jsonl``
— one JSON record per line. The reader here walks that tree on demand and
returns ``Conversation`` records sorted by ``start_time`` descending.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("claude_code_proxy.conversations.reader")


class ConversationMessage(BaseModel):
    """One JSONL record from a Claude Code session file.

    The raw ``line`` is preserved so the dashboard can render the original
    payload even if our flat fields don't capture every detail.
    """

    model_config = ConfigDict(extra="allow")

    role: str | None = None
    content: str | list[Any] | dict[str, Any] | None = None
    timestamp: datetime | None = None
    type: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Conversation(BaseModel):
    """One Claude Code session, parsed from a single .jsonl file."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session_id: str
    project_path: str  # the segment under .claude/projects/, e.g. "-home-craig-foo"
    project_name: str  # last path component, e.g. "foo"
    messages: list[ConversationMessage] = Field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None
    message_count: int = 0
    file_path: Path


class ConversationReader:
    """Walk ``base_path/<project>/<session>.jsonl`` and produce Conversations."""

    def __init__(self, base_path: Path | None = None) -> None:
        self.base = Path(base_path) if base_path else Path.home() / ".claude" / "projects"

    # ---- listing ----------------------------------------------------------

    def list_conversations(
        self, *, page: int = 1, limit: int = 10
    ) -> tuple[list[Conversation], int]:
        """All sessions across all projects, sorted by ``start_time`` DESC.

        Returns ``(rows, total)`` — pagination is applied in Python after
        the full scan; for tens of thousands of sessions this should be
        revisited (e.g. cache a manifest sorted by file mtime).
        """
        all_conversations: list[Conversation] = []
        if not self.base.is_dir():
            return [], 0

        for jsonl in self.base.rglob("*.jsonl"):
            conv = self._build_metadata(jsonl)
            if conv is not None:
                all_conversations.append(conv)

        all_conversations.sort(
            key=lambda c: c.start_time or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        total = len(all_conversations)
        page = max(1, page)
        limit = max(1, limit)
        start = (page - 1) * limit
        end = start + limit
        return all_conversations[start:end], total

    def get_conversations_by_project(self, project_path: str) -> list[Conversation]:
        """All sessions under ``base/<project_path>/``."""
        proj_dir = self.base / project_path
        if not proj_dir.is_dir():
            return []
        out: list[Conversation] = []
        for jsonl in proj_dir.glob("*.jsonl"):
            conv = self._build_metadata(jsonl)
            if conv is not None:
                out.append(conv)
        out.sort(
            key=lambda c: c.start_time or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return out

    def get_conversation(self, session_id: str, project_path: str) -> Conversation | None:
        """Parse a single session's JSONL fully (every message)."""
        path = self.base / project_path / f"{session_id}.jsonl"
        if not path.is_file():
            return None
        return self._build_full(path)

    # ---- internals --------------------------------------------------------

    def _build_metadata(self, path: Path) -> Conversation | None:
        """Build a Conversation from the first/last records of a session.

        Reads only the first and last lines to compute start_time/end_time
        cheaply — full message bodies aren't loaded until ``get_conversation``
        is called.
        """
        try:
            with path.open(encoding="utf-8") as f:
                first_line = f.readline()
                # Seek to last non-empty line.
                lines = f.readlines()
                last_line = lines[-1] if lines else first_line
        except OSError as e:
            logger.warning("could not read %s: %s", path, e)
            return None

        first = _parse_line(first_line)
        last = _parse_line(last_line)

        return Conversation(
            session_id=path.stem,
            project_path=path.parent.name,
            project_name=path.parent.name,
            messages=[],
            start_time=(first.timestamp if first else None),
            end_time=(last.timestamp if last else None),
            message_count=0,  # populated only by _build_full
            file_path=path,
        )

    def _build_full(self, path: Path) -> Conversation:
        """Parse every line of the JSONL file."""
        messages: list[ConversationMessage] = []
        start: datetime | None = None
        end: datetime | None = None
        try:
            with path.open(encoding="utf-8") as f:
                for raw_line in f:
                    msg = _parse_line(raw_line)
                    if msg is None:
                        continue
                    messages.append(msg)
                    if start is None or (msg.timestamp and msg.timestamp < start):
                        start = msg.timestamp
                    if end is None or (msg.timestamp and msg.timestamp > end):
                        end = msg.timestamp
        except OSError as e:
            logger.warning("could not read %s: %s", path, e)

        return Conversation(
            session_id=path.stem,
            project_path=path.parent.name,
            project_name=path.parent.name,
            messages=messages,
            start_time=start,
            end_time=end,
            message_count=len(messages),
            file_path=path,
        )


def _parse_line(line: str) -> ConversationMessage | None:
    """Parse one JSONL line into a ConversationMessage.

    Skips malformed lines silently — a single bad record shouldn't break
    the whole session.
    """
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    msg = raw.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}

    role = msg.get("role") or raw.get("role")
    content = msg.get("content") if "content" in msg else None

    return ConversationMessage(
        role=role,
        content=content,
        timestamp=_parse_dt(raw.get("timestamp")),
        type=raw.get("type"),
        raw=raw,
    )


def _parse_dt(value: Any) -> datetime | None:
    """Parse the timestamp fields Claude Code emits into a ``datetime``."""
    if value is None or not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
