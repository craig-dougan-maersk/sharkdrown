"""Persistent configuration for the embedded proxy.

Stored via :class:`PySide6.QtCore.QSettings`, which uses platform-native
storage (registry on Windows, plist on macOS, INI under
``~/.config/<Organization>/<Application>`` on Linux). Reads fall back to
the dataclass defaults when keys are absent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSettings


@dataclass(frozen=True)
class ProxyConfig:
    """User-tunable proxy settings.

    Attributes
    ----------
    host:
        Bind address. Defaults to ``127.0.0.1`` (loopback only) so the
        desktop never exposes the proxy on the LAN by accident.
    port:
        TCP port to bind. ``0`` means "let the OS pick a free port" — the
        actual bound port is reported via ``ProxyServerThread.started_ok``.
    autostart:
        If True, the proxy is started when the MainWindow is constructed.
    config_dir:
        Directory where the proxy's ``config.yaml`` and ``.env`` files
        live. The vendored proxy reads these on startup; we discover them
        here. Empty string means "auto-discover" (search data dir, then
        CWD, then the original source-proxy location).
    default_endpoint:
        Fallback upstream URL used when an incoming request's model is
        not in ``model_endpoints``. ``None`` falls back to whatever
        ``providers.anthropic.base_url`` / ``providers.openai.base_url``
        is configured in the proxy's own config (the original behaviour).
    model_endpoints:
        Mapping of model name → upstream URL. When a request's model
        appears in this dict, the proxy forwards to that URL instead of
        the provider's configured ``base_url``. Useful for routing
        specific Claude Code models to alternative upstreams.
    """

    host: str = "127.0.0.1"
    port: int = 3001
    autostart: bool = False
    config_dir: str = ""
    default_endpoint: str = ""
    model_endpoints: dict[str, str] = field(default_factory=dict)

    # ---- QSettings plumbing ---------------------------------------------
    _KEY_HOST = "proxy/host"
    _KEY_PORT = "proxy/port"
    _KEY_AUTOSTART = "proxy/autostart"
    _KEY_CONFIG_DIR = "proxy/configDir"
    _KEY_DEFAULT_ENDPOINT = "proxy/defaultEndpoint"
    _KEY_MODEL_ENDPOINTS = "proxy/modelEndpoints"  # stored as JSON string

    @classmethod
    def load(cls) -> "ProxyConfig":
        s = QSettings()
        raw_endpoints = s.value(cls._KEY_MODEL_ENDPOINTS, "", type=str)
        model_endpoints: dict[str, str] = {}
        if raw_endpoints:
            try:
                parsed = json.loads(raw_endpoints)
                if isinstance(parsed, dict):
                    model_endpoints = {
                        str(k): str(v) for k, v in parsed.items() if v
                    }
            except json.JSONDecodeError:
                # Corrupt value — ignore and start fresh.
                pass
        return cls(
            host=s.value(cls._KEY_HOST, cls.host, type=str),
            port=int(s.value(cls._KEY_PORT, cls.port, type=int)),
            autostart=s.value(cls._KEY_AUTOSTART, cls.autostart, type=bool),
            config_dir=s.value(cls._KEY_CONFIG_DIR, "", type=str),
            default_endpoint=s.value(cls._KEY_DEFAULT_ENDPOINT, "", type=str),
            model_endpoints=model_endpoints,
        )

    def save(self) -> None:
        s = QSettings()
        s.setValue(self._KEY_HOST, self.host)
        s.setValue(self._KEY_PORT, self.port)
        s.setValue(self._KEY_AUTOSTART, self.autostart)
        s.setValue(self._KEY_CONFIG_DIR, self.config_dir)
        s.setValue(self._KEY_DEFAULT_ENDPOINT, self.default_endpoint)
        s.setValue(self._KEY_MODEL_ENDPOINTS, json.dumps(self.model_endpoints))
        s.sync()

    @property
    def base_url(self) -> str:
        """URL that Claude Code CLI should point at."""
        return f"http://{self.host}:{self.port}"

    @property
    def config_dir_path(self) -> Path | None:
        """Resolved Path to the user's config dir, or None for auto-discover."""
        return Path(self.config_dir) if self.config_dir else None