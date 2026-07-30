"""Shared pytest fixtures for SharkDrown test suite.

Provides common infrastructure so individual test files don't need to
duplicate QApplication setup, free port discovery, temp directories, or
httpx test client construction.

Usage:
    Tests can use these fixtures by name — pytest discovers conftest.py
    automatically. Existing tests that create their own QApplication
    still work (the ``qapp`` fixture reuses any existing instance).
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

# Force offscreen rendering for all tests (headless CI, no display server).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication instance (reuses existing if present)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture()
def free_port() -> int:
    """Return an unused TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def tmp_dir(tmp_path: Path) -> Path:
    """Return a fresh temporary directory (pytest-managed, auto-cleaned)."""
    return tmp_path


@pytest.fixture()
def isolated_settings(tmp_path: Path):
    """Provide an isolated QSettings store that won't trample user config.

    Yields the temp path used for QSettings. Restores original settings
    on teardown.
    """
    from PySide6.QtCore import QSettings

    original_path = QSettings.defaultFormat()
    settings_path = tmp_path / "settings"
    settings_path.mkdir()
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(settings_path))
    yield settings_path
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, "")


@pytest.fixture()
def storage_service(tmp_path: Path):
    """Create a StorageService with an isolated SQLite database."""
    import asyncio

    from ccproxy.app.db.storage import StorageService

    db_path = tmp_path / "test.db"
    service = StorageService(db_path=db_path)
    asyncio.run(service.connect())
    yield service
    asyncio.run(service.close())


@pytest.fixture()
def proxy_app(tmp_path: Path):
    """Create an in-process FastAPI app for testing proxy endpoints.

    Returns the app instance suitable for use with httpx.ASGITransport.
    """
    # Add src/ to path for ccproxy imports
    src_dir = Path(__file__).parent.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from ccproxy.app.main import create_app

    app = create_app(storage_path=tmp_path / "proxy-test.db")
    return app


@pytest.fixture()
def credential_store(tmp_path: Path):
    """Create an isolated CredentialStore for testing."""
    from src.profiles.credential_store import CredentialStore

    return CredentialStore(tmp_path / "credentials.json")


@pytest.fixture()
def profile_store(tmp_path: Path):
    """Create an isolated ProfileStore for testing."""
    from src.profiles.credential_store import CredentialStore
    from src.profiles.store import ProfileStore

    creds = CredentialStore(tmp_path / "credentials.json")
    return ProfileStore(tmp_path / "profiles.json", credential_store=creds)
