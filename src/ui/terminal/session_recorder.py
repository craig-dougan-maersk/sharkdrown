"""Terminal session recorder in asciicast v2 format.

Records timestamped PTY output for later replay. The output file is
compatible with asciinema players and the `.cast` format.

Asciicast v2 format:
  Line 1: JSON header with version, width, height, timestamp
  Lines 2+: [elapsed_seconds, "o", base64_or_text_data]

Usage:
    recorder = SessionRecorder(path="session.cast", cols=80, rows=24)
    recorder.start()
    # connect to pty_session.data_received signal:
    pty_session.data_received.connect(recorder.record)
    # ... session runs ...
    recorder.stop()
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class SessionRecorder:
    """Records terminal output to an asciicast v2 file."""

    def __init__(
        self,
        path: str | Path,
        *,
        cols: int = 80,
        rows: int = 24,
        title: str | None = None,
    ) -> None:
        self._path = Path(path)
        self._cols = cols
        self._rows = rows
        self._title = title
        self._file = None
        self._start_time: float = 0.0
        self._recording = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        """Begin recording. Writes the header line."""
        if self._recording:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "w", encoding="utf-8")
        self._start_time = time.monotonic()

        header = {
            "version": 2,
            "width": self._cols,
            "height": self._rows,
            "timestamp": int(time.time()),
        }
        if self._title:
            header["title"] = self._title
        header["env"] = {"TERM": "xterm-256color", "SHELL": "/bin/zsh"}

        self._file.write(json.dumps(header) + "\n")
        self._file.flush()
        self._recording = True

    def record(self, data: bytes) -> None:
        """Record a chunk of PTY output. Connect to PtySession.data_received."""
        if not self._recording or self._file is None:
            return
        elapsed = time.monotonic() - self._start_time
        text = data.decode("utf-8", errors="replace")
        line = json.dumps([round(elapsed, 6), "o", text])
        self._file.write(line + "\n")
        self._file.flush()

    def stop(self) -> None:
        """Stop recording and close the file."""
        if not self._recording:
            return
        self._recording = False
        if self._file is not None:
            self._file.close()
            self._file = None

    @property
    def duration(self) -> float:
        """Elapsed time since recording started (seconds)."""
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time
