"""Cross-platform paths for the embedded proxy.

These helpers rely on ``QStandardPaths.AppDataLocation`` which requires the
QApplication to have ``setOrganizationName`` and ``setApplicationName`` set
in ``src/main.py`` before any of these are called.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QStandardPaths


def data_dir() -> Path:
    """Return (and create) the per-user app-data directory.

    Resolves to:
      - Linux:  ~/.local/share/Sharkdrown/PandasMarkdownEditor
      - macOS:  ~/Library/Application Support/Sharkdrown/PandasMarkdownEditor
      - Windows: %APPDATA%/Sharkdrown/PandasMarkdownEditor
    """
    base = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_db_path() -> Path:
    """Path to the SQLite capture database used by the proxy."""
    return data_dir() / "ccproxy-requests.db"


def default_config_path() -> Path:
    """Where the desktop should look for an optional ``config.yaml``.

    Mirrors the proxy's default config location but inside the per-user
    app-data directory so users don't need to touch system paths.
    """
    return data_dir() / "config.yaml"


# ---- Config discovery -----------------------------------------------------

# Common parent directories under $HOME where users typically clone repos.
# Used only as a final fallback when neither the data dir nor CWD has a
# config.yaml. /srv is intentionally excluded — it is a system directory on
# Linux and should never be scanned for user config.
_KNOWN_PROXY_PARENTS = (
    Path.home() / "code",
    Path.home() / "projects",
    Path.home() / "src",
    Path.home() / "work",
)


def _candidate_config_dirs() -> list[Path]:
    """Return candidate directories that might contain proxy config files.

    Order matters — earlier entries win. We search:
      1. The user-configured config dir (passed in explicitly).
      2. The desktop's app-data directory.
      3. The current working directory.
      4. Known parent directories containing ``claude-code-proxy-py``.
    """
    cwd = Path(os.getcwd())
    candidates: list[Path] = []
    candidates.append(data_dir())
    candidates.append(cwd)
    for parent in _KNOWN_PROXY_PARENTS:
        if parent.is_dir():
            for child in parent.iterdir():
                if child.is_dir() and child.name == "claude-code-proxy-py":
                    candidates.append(child)
    # Deduplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        try:
            key = c.resolve()
        except OSError:
            key = c
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def find_config_yaml(config_dir: Path | None = None) -> Path | None:
    """Locate ``config.yaml`` for the embedded proxy.

    If ``config_dir`` is provided, look ONLY in that directory (return
    ``None`` if not found). Otherwise, search the candidate dirs from
    :func:`_candidate_config_dirs` in order and return the first match.

    The caller passes the resulting path to ``Settings(config_yaml_path=...)``.
    """
    if config_dir is not None:
        candidate = config_dir / "config.yaml"
        return candidate if candidate.is_file() else None
    for d in _candidate_config_dirs():
        candidate = d / "config.yaml"
        if candidate.is_file():
            return candidate
    return None


def find_env_file(config_dir: Path | None = None) -> Path | None:
    """Locate ``.env`` for the embedded proxy.

    Same discovery semantics as :func:`find_config_yaml`. The caller is
    expected to call ``dotenv.load_dotenv(path)`` on the returned path
    before constructing ``Settings`` so the proxy's pydantic-settings
    env layer picks up the values.
    """
    if config_dir is not None:
        candidate = config_dir / ".env"
        return candidate if candidate.is_file() else None
    for d in _candidate_config_dirs():
        candidate = d / ".env"
        if candidate.is_file():
            return candidate
    return None