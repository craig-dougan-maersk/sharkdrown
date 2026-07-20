"""Embedded Claude Code proxy integration.

This package glues the vendored FastAPI proxy (``src/ccproxy/``) into the
PySide6 desktop app. Public surface:

    src/proxy/paths.py            — QStandardPaths helpers
    src/proxy/proxy_config.py     — QSettings-backed config dataclass
    src/proxy/server_thread.py    — QThread running uvicorn in an asyncio loop
    src/proxy/dashboard_view.py   — QWebEngineView wrapper for the embedded UI
    src/proxy/settings_dialog.py  — modal dialog to edit ProxyConfig

The desktop's ``MainWindow`` owns the lifecycle of ``ProxyServerThread`` and
the ``QDockWidget`` that hosts ``DashboardView``.
"""

__all__ = [
    "paths",
    "proxy_config",
    "server_thread",
    "dashboard_view",
    "settings_dialog",
]