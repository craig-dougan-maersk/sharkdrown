"""Run the embedded FastAPI proxy inside a QThread.

The thread owns its own asyncio loop (``asyncio.run`` inside ``run()``)
so uvicorn's internals never touch Qt's GUI loop. The thread exposes
signals that the MainWindow slots into to update its UI.

Lifecycle
---------
``start()`` → ``run()`` → ``asyncio.run(self._serve())`` → exits when
``server.should_exit`` is set via ``request_stop()`` or uvicorn crashes.

The lifespan in ``ccproxy.app.main`` opens SQLite at startup and closes it
during shutdown's finally block, so all DB cleanup happens automatically
before this thread exits.

Configuration discovery
-----------------------
The vendored proxy reads ``config.yaml`` and ``.env`` from CWD when run
standalone. The embedded version must discover them in alternate locations
(data dir, the original source-proxy path) because the desktop's CWD is
typically not the proxy project. See ``src.proxy.paths`` for the search
order; the resolved paths are passed to ``Settings(config_yaml_path=...)``
and ``dotenv.load_dotenv(...)`` before ``create_app`` is called.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from PySide6.QtCore import QThread, Signal

# Make src/ importable so ``from ccproxy.app.main import create_app`` works
# regardless of how the desktop app is launched (entrypoint script, pytest,
# packaged binary, etc.).
_PROXY_PARENT = Path(__file__).resolve().parents[1]  # .../src
if str(_PROXY_PARENT) not in sys.path:
    sys.path.insert(0, str(_PROXY_PARENT))

from ccproxy.app.config import Settings  # noqa: E402
from ccproxy.app.main import create_app  # noqa: E402

from src.proxy.paths import find_config_yaml, find_env_file  # noqa: E402

logger = logging.getLogger("sharkdrown.proxy")


class ProxyServerThread(QThread):
    """A QThread that runs uvicorn with its own asyncio loop.

    Signals
    -------
    started_ok(int):
        Emitted once the server is listening. The int is the bound port
        (useful when the user asked for ``port=0``).
    failed(str):
        Emitted if ``create_app()`` or uvicorn fails to start.
    stopped():
        Emitted after the server has fully torn down (SQLite closed,
        AsyncClients closed).
    """

    started_ok = Signal(int)
    failed = Signal(str)
    stopped = Signal()

    def __init__(
        self,
        *,
        host: str,
        port: int,
        storage_path: Path,
        config_dir: Path | None = None,
        model_endpoints: dict[str, str] | None = None,
        default_endpoint: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port
        self._storage_path = Path(storage_path)
        self._config_dir = config_dir
        # ``model_endpoints`` and ``default_endpoint`` come from the
        # user's settings dialog; they're applied on top of any
        # existing config via a custom Settings instance.
        self._model_endpoints = dict(model_endpoints or {})
        self._default_endpoint = default_endpoint or None
        self._server: uvicorn.Server | None = None

    # -- QThread entry point ----------------------------------------------
    def run(self) -> None:
        """Override QThread.run so we control the asyncio loop directly.

        ``asyncio.run()`` creates a private loop for this thread, runs the
        coroutine to completion, and closes the loop — guaranteeing no
        leaked tasks. Qt's GUI loop is untouched.
        """
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 — surface any startup error
            logger.exception("ProxyServerThread crashed")
            self.failed.emit(f"server crashed: {exc}")

    async def _serve(self) -> None:
        try:
            settings = self._build_settings()
            app = create_app(settings=settings, storage_path=self._storage_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("create_app failed")
            self.failed.emit(f"create_app failed: {exc}")
            return

        config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            log_level="info",
            lifespan="on",
            # Disable reload — QThread re-exec would create a fork we
            # don't want. Hot reload belongs to dev-only workflows.
            reload=False,
            # Cap graceful shutdown at 5s. Without this, an open SSE
            # connection (the dashboard's /stream listener) keeps uvicorn
            # waiting forever. The dashboard's EventSource will reconnect
            # automatically on next start.
            timeout_graceful_shutdown=5,
        )
        server = uvicorn.Server(config)
        # Don't let uvicorn register SIGTERM/SIGINT handlers — Qt owns the
        # process signal handling in a desktop app. Without this we'd
        # race the GUI loop's signal disposition on shutdown.
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._server = server

        serve_task = asyncio.create_task(server.serve())
        try:
            # Wait for uvicorn to finish startup (or fail) before emitting.
            while not serve_task.done():
                if server.started:
                    self.started_ok.emit(self._port)
                    break
                await asyncio.sleep(0.05)
            # If startup failed before ``started`` flipped, surface the error.
            if not server.started:
                try:
                    await serve_task
                except Exception as exc:  # noqa: BLE001
                    logger.exception("startup failed before started")
                    self.failed.emit(f"startup failed: {exc}")
                    return
                self.failed.emit("server exited before becoming ready")
                return
            # Now just wait for uvicorn to drain on shutdown.
            await serve_task
        except Exception as exc:  # noqa: BLE001
            logger.exception("uvicorn crashed")
            self.failed.emit(f"uvicorn crashed: {exc}")
            return
        finally:
            self._server = None
            self.stopped.emit()

    # -- Configuration -----------------------------------------------------
    def _build_settings(self) -> Settings | None:
        """Build a Settings instance wired to the discovered config files.

        Returns ``None`` if no config files were found, in which case
        ``create_app`` will fall back to defaults — useful for first-run
        users who haven't set anything up yet.

        The order of operations matters:
          1. ``dotenv.load_dotenv`` populates ``os.environ`` with any
             ``KEY=value`` lines from ``.env``. We pass ``override=False``
             so existing shell env vars (e.g. ``ANTHROPIC_AUTH_TOKEN``)
             keep precedence.
          2. ``Settings(config_yaml_path=...)`` then reads YAML on top of
             the now-populated environment.
          3. The user's per-model endpoint overrides (from the desktop
             settings dialog) are layered on top so they take precedence
             over anything in the YAML.
        """
        env_path = find_env_file(self._config_dir)
        if env_path is not None:
            logger.info("loading proxy env from %s", env_path)
            # python-dotenv is already a dep of fastapi/pydantic-settings
            import dotenv

            dotenv.load_dotenv(env_path, override=False)

        yaml_path = find_config_yaml(self._config_dir)
        settings: Settings | None
        if yaml_path is not None:
            logger.info("loading proxy config from %s", yaml_path)
            settings = Settings(config_yaml_path=yaml_path)
        elif env_path is not None:
            logger.info("no config.yaml found; using env + defaults")
            settings = None
        else:
            logger.warning(
                "no proxy config.yaml or .env found; using defaults "
                "(upstream will be api.anthropic.com)"
            )
            settings = None

        # Layer in the per-model endpoint overrides. We replace the
        # ``model_endpoints`` field wholesale because the user explicitly
        # configured this in the desktop's settings dialog — it should
        # not be silently merged with anything the YAML happens to have.
        if settings is not None and (self._model_endpoints or self._default_endpoint):
            from ccproxy.app.config import ModelEndpointsCfg

            settings.model_endpoints = ModelEndpointsCfg(
                default_endpoint=self._default_endpoint,
                endpoints=dict(self._model_endpoints),
            )
            logger.info(
                "applied per-model endpoint overrides: %d model(s), default=%s",
                len(self._model_endpoints),
                self._default_endpoint or "(none)",
            )
        return settings

    # -- Public control ---------------------------------------------------
    def request_stop(self) -> None:
        """Thread-safe: ask uvicorn to exit on its next loop tick.

        uvicorn checks ``should_exit`` between requests, so this returns
        immediately and the actual shutdown happens asynchronously inside
        ``_serve``. Safe to call from the GUI thread.
        """
        server = self._server
        if server is not None:
            server.should_exit = True

    # -- Introspection ----------------------------------------------------
    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def storage_path(self) -> Path:
        return self._storage_path

    @property
    def config_dir(self) -> Path | None:
        return self._config_dir