"""FastAPI application entrypoint.

The application is constructed via a factory pattern so tests can pass in
mock providers, a custom settings file, or a tmp SQLite path. The lifespan
context manager wires up Settings, providers, storage, and the subagent
router at startup, and tears them down on shutdown.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ccproxy.app.config import Settings
from ccproxy.app.db.storage import StorageService
from ccproxy.app.live.bus import EventBus
from ccproxy.app.middleware import AccessLogMiddleware, BodyCaptureMiddleware, RateLimitMiddleware
from ccproxy.app.providers import AnthropicProvider, OpenAIProvider, Provider
from ccproxy.app.router.subagent import SubagentRouter

logger = logging.getLogger("claude_code_proxy")


def _default_providers(settings: Settings) -> dict[str, Provider]:
    """Construct the upstream providers from settings.

    Returns a dict keyed by provider name with concrete
    ``AnthropicProvider`` / ``OpenAIProvider`` instances. The
    router looks providers up by name (``self._providers.get(name)``),
    so every user-defined provider in
    ``settings.model_endpoints.providers`` must be materialised
    here — otherwise the router's lookup returns ``None`` and falls
    through to the hardcoded ``anthropic`` default, sending the
    user's request to ``api.anthropic.com`` even when they
    explicitly routed the model elsewhere.

    Two layers:
      - Built-in providers (``anthropic``, ``openai``) come from
        ``settings.providers`` (read from YAML config).
      - User-defined providers in
        ``settings.model_endpoints.providers`` are added as
        ``AnthropicProvider`` instances (all current upstream
        gateways use Anthropic's wire format). Each one gets its
        own HTTP client so closing one doesn't disrupt another.
    """
    providers: dict[str, Provider] = {
        "anthropic": AnthropicProvider(
            base_url=str(settings.providers.anthropic.base_url),
            version=settings.providers.anthropic.version,
        ),
        "openai": OpenAIProvider(
            base_url=str(settings.providers.openai.base_url),
            api_key=settings.providers.openai.api_key.get_secret_value(),
        ),
    }
    # User-defined providers (per-profile). All current AI gateways
    # we route through (MiniMax, OpenRouter, etc.) speak
    # Anthropic's wire format, so we instantiate AnthropicProvider
    # for each one. If a future provider needs a different wire
    # format, we'd key off ``provider_cfg.kind`` (or similar) here.
    # The api_key lives on ``ProviderCfg`` and is plumbed through
    # ``target_api_key`` per-request, not on the constructor.
    model_endpoints_cfg = settings.model_endpoints
    if model_endpoints_cfg and model_endpoints_cfg.providers:
        for prov_name, prov_cfg in model_endpoints_cfg.providers.items():
            if prov_name in providers:
                # Don't shadow the built-in anthropic/openai with a
                # user-defined same-named provider — first writer
                # wins; the user can rename via the profile editor
                # if they want a different name.
                continue
            providers[prov_name] = AnthropicProvider(
                base_url=prov_cfg.url,
            )

    # Discover drop-in provider plugins from the plugins directory and
    # entry points. Plugins don't override built-in or config providers.
    from ccproxy.app.providers.discovery import discover_plugins

    plugin_dir = Path(__file__).parent / "providers" / "plugins"
    plugins = discover_plugins(directory=plugin_dir)
    for name, instance in plugins.items():
        if name not in providers:
            providers[name] = instance

    return providers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire up Settings, providers, storage, router; tear down on exit."""
    settings: Settings = app.state.settings
    storage: StorageService = app.state.storage
    providers: dict[str, Provider] = app.state.providers

    # Set up the live-update event bus. If the storage service was created
    # before this code (e.g. by tests), we attach the bus to it now.
    event_bus = EventBus()
    app.state.event_bus = event_bus
    storage._event_bus = event_bus

    await storage.connect()
    app.state.router = SubagentRouter(settings, providers)
    logger.info(
        "starting claude-code-proxy-py: model=%s, providers=%s, subagents.enable=%s",
        "single-process",
        sorted(providers.keys()),
        settings.subagents.enable,
    )
    # Surface the effective upstream URLs at startup so misrouting (e.g. an
    # env-var override silently beating config.yaml) is obvious in the log.
    # We bypass the Python logging module entirely: uvicorn's dictConfig
    # only attaches a handler to a few specific loggers, and routing our
    # ``logger.info`` calls through that setup reliably is fiddly. Writing
    # directly to stderr puts the line on the same stream as uvicorn's own
    # output, so ``podman logs`` shows it next to the rest of the startup.
    print(
        "starting claude-code-proxy-py: "
        f"providers={sorted(providers.keys())} "
        f"subagents.enable={settings.subagents.enable}",
        file=sys.stderr,
    )
    for name, p in providers.items():
        base = getattr(p, "_base_url", None)
        if base:
            print(f"provider {name} -> {base}", file=sys.stderr)
    try:
        yield
    finally:
        await storage.close()
        # Close HTTP clients on each provider (they may be shared).
        for provider in providers.values():
            close = getattr(provider, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001 — provider close is best-effort cleanup
                    logger.warning(
                        "provider %r close failed",
                        getattr(provider, "name", provider),
                        exc_info=True,
                    )
        logger.info("stopping claude-code-proxy-py")


def create_app(
    *,
    settings: Settings | None = None,
    providers: dict[str, Provider] | None = None,
    storage_path: Path | str | None = None,
) -> FastAPI:
    """Build a FastAPI instance with full lifespan wiring.

    Parameters
    ----------
    settings:
        Override Settings construction. If None, build from the default
        sources (env, dotenv, config.yaml).
    providers:
        Inject pre-built providers (used by tests with httpx.MockTransport).
        If None, real providers are built from settings.
    storage_path:
        Override the SQLite file path. Useful for tests using tmp_path.
    """
    app = FastAPI(
        title="Claude Code Proxy",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Build settings eagerly; we need them on app.state before lifespan runs
    # because tests introspect app.state.settings directly.
    if settings is None:
        settings = Settings()
    app.state.settings = settings

    # Build providers.
    if providers is None:
        providers = _default_providers(settings)
    app.state.providers = providers

    # Storage — lazy-connect via lifespan. The event bus is attached
    # during the lifespan startup so storage has a way to publish events.
    if storage_path is None:
        storage_path = settings.storage.db_path
    app.state.storage = StorageService(db_path=storage_path)

    # CORS — restrict to localhost origins to prevent DNS rebinding attacks.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    # Body capture must precede access log (so the log can mention body size).
    app.add_middleware(BodyCaptureMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AccessLogMiddleware)

    # Mount routes.
    from ccproxy.app.api.conversations import router as conversations_router
    from ccproxy.app.api.dashboard import router as dashboard_router
    from ccproxy.app.api.live import router as live_router
    from ccproxy.app.api.monitor import router as monitor_router
    from ccproxy.app.api.proxy import router as proxy_router
    from ccproxy.app.api.system import router as system_router

    app.include_router(system_router)
    app.include_router(proxy_router)
    app.include_router(monitor_router)
    app.include_router(conversations_router)
    app.include_router(live_router)
    app.include_router(dashboard_router)

    # Templates + static assets for the dashboard.
    template_dir = Path(__file__).parent / "templates"
    app.state.templates = Jinja2Templates(directory=str(template_dir))

    from ccproxy.app.pricing import compute_cost

    app.state.templates.env.globals["compute_cost"] = compute_cost

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


# uvicorn entrypoint: `uvicorn ccproxy.app.main:app` (when this module is loaded
# as the top-level ASGI app). We deliberately do NOT call ``create_app()`` here —
# the embedded Qt app invokes ``create_app()`` explicitly inside
# ``src/proxy/server_thread.py`` with controlled arguments (storage_path, etc.).
# Eagerly constructing the app would open SQLite, instantiate two AsyncClients,
# and read config.yaml from CWD before the GUI has a chance to configure paths.
app = None  # deferred; use create_app()
