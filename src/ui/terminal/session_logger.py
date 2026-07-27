"""Plain-text session logger for terminal output.

Strips ANSI escape sequences and writes readable text to a timestamped
log file. Each terminal/Claude session gets its own file, named with the
epoch timestamp to avoid conflicts between concurrent sessions.

Log directory: ~/.sharkdrown/logs/
Filename pattern: {label}_{epoch}.log
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;?]*[A-Za-z]"  # CSI sequences (colors, cursor moves, etc.)
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (title sets, etc.)
    r"|\([A-Z0-9]"  # Character set designation
    r"|[=>NH]"  # Simple escape codes
    r")"
)

_DEFAULT_LOG_DIR = Path.home() / ".sharkdrown" / "logs"


class SessionLogger:
    """Logs terminal output as plain text (ANSI stripped) to a file."""

    def __init__(
        self,
        log_dir: Path | str | None = None,
        session_label: str = "terminal",
    ) -> None:
        log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
        epoch = int(time.time())
        self._path = log_dir / f"{session_label}_{epoch}.log"
        self._file = None
        self._enabled = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def start(self) -> Path:
        """Open the log file and begin recording."""
        if self._enabled:
            return self._path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")
        self._enabled = True
        return self._path

    def write(self, data: bytes) -> None:
        """Strip ANSI escapes from data and append to the log file."""
        if not self._enabled or self._file is None:
            return
        text = data.decode("utf-8", errors="replace")
        clean = _ANSI_RE.sub("", text)
        if clean:
            self._file.write(clean)
            if "\n" in clean:
                self._file.flush()

    def stop(self) -> None:
        """Close the log file."""
        if not self._enabled:
            return
        self._enabled = False
        if self._file is not None:
            self._file.close()
            self._file = None
