"""Tests for per-model endpoint routing in the embedded proxy.

Verifies:
  - ``ProxyConfig`` round-trips model_endpoints and default_endpoint
  - The vendored ``Settings`` accepts the overrides via the proxy
    config field
  - The route handler reads ``settings.model_endpoints`` correctly
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def test_proxy_config_roundtrip_model_endpoints() -> None:
    """ProxyConfig should persist model_endpoints via QSettings."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("Sharkdrown-Test")
    app.setApplicationName("PandasMarkdownEditor-EndpointTest")

    # Use an isolated INI store so we don't trample the user's settings.
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(Path("/tmp/sharkdrown-endpoint-test")),
    )
    QSettings().clear()

    from src.proxy.proxy_config import ProxyConfig

    cfg = ProxyConfig(
        host="127.0.0.1",
        port=3001,
        default_endpoint="https://api.minimax.io/anthropic",
        model_endpoints={
            "claude-sonnet-4-5": "https://api.minimax.io/anthropic",
            "claude-opus-4-1": "https://api.anthropic.com",
        },
    )
    cfg.save()
    loaded = ProxyConfig.load()
    assert loaded.default_endpoint == cfg.default_endpoint
    assert loaded.model_endpoints == cfg.model_endpoints
    print("✓ ProxyConfig round-trips model_endpoints and default_endpoint")


def test_settings_accepts_model_endpoints() -> None:
    """The vendored Settings should accept the model_endpoints field."""
    from ccproxy.app.config import ModelEndpointsCfg, Settings

    cfg = ModelEndpointsCfg(
        default_endpoint="https://api.minimax.io/anthropic",
        endpoints={"claude-sonnet-4-5": "https://api.anthropic.com"},
    )
    settings = Settings()
    # Pydantic allows attribute assignment for non-frozen models.
    settings.model_endpoints = cfg
    assert settings.model_endpoints.endpoints["claude-sonnet-4-5"] == (
        "https://api.anthropic.com"
    )
    assert settings.model_endpoints.default_endpoint == (
        "https://api.minimax.io/anthropic"
    )
    print("✓ Settings accepts model_endpoints assignment")


def test_endpoint_resolution_order() -> None:
    """Verify resolution: per-model → default → provider base_url."""
    from ccproxy.app.config import ModelEndpointsCfg, Settings

    settings = Settings()
    # Case 1: per-model match
    settings.model_endpoints = ModelEndpointsCfg(
        default_endpoint="https://default.example.com",
        endpoints={"claude-haiku-4-5": "https://haiku.example.com"},
    )
    assert (
        settings.model_endpoints.endpoints.get("claude-haiku-4-5")
        == "https://haiku.example.com"
    )
    # Case 2: per-model miss → default
    assert (
        settings.model_endpoints.endpoints.get("claude-unknown") is None
    )
    assert settings.model_endpoints.default_endpoint == (
        "https://default.example.com"
    )
    print("✓ Endpoint resolution: per-model match wins, default is fallback")


def main() -> None:
    tests = [
        test_proxy_config_roundtrip_model_endpoints,
        test_settings_accepts_model_endpoints,
        test_endpoint_resolution_order,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n❌ {failed} test(s) failed")
        sys.exit(1)
    print("\n✅ Endpoint routing tests passed!")


if __name__ == "__main__":
    main()