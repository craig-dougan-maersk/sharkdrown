"""Main window package — refactored into per-concern mixin modules.

Public API:
    MainWindow — the application's top-level QMainWindow
"""

from src.ui.main_window.window import MainWindow
from src.proxy.server_thread import ProxyServerThread

__all__ = ["MainWindow", "ProxyServerThread"]
