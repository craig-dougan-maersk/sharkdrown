"""Application configuration.

Settings load from four sources, highest to lowest precedence:

    1. Init args passed to ``Settings(...)``
    2. Environment variables (uppercase, see ``env = ...`` on each field)
    3. ``config.yaml`` (via the custom YAML settings source)
    4. ``.env`` (via pydantic-settings' built-in dotenv source)
    5. Field defaults

Timeouts accept either an int (seconds) or a duration string ("10m", "30s",
"1h"). The string form is normalized to seconds at parse time so the runtime
never sees inconsistent units.

The ``subagent_mappings`` env var is a CSV ("code-reviewer:gpt-4o,data-analyst:o3")
that merges into ``subagents.mappings`` before validation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger("claude_code_proxy.config")

# ---- Timeout coercion -------------------------------------------------------

_TIMEOUT_UNITS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TIMEOUT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def _coerce_timeout(v: Any) -> int:
    """Normalize a timeout value to integer seconds.

    Accepts:
      - ``int``           → returned as-is (seconds)
      - ``"10m"``         → ``600``
      - ``"30s"``         → ``30``
      - ``"1.5h"``       → ``5400``
      - ``"600"``         → ``600`` (bare int string)
    """
    if isinstance(v, bool):  # bool is subclass of int; reject explicitly
        raise ValueError("timeout cannot be a boolean")
    if isinstance(v, int):
        if v < 0:
            raise ValueError(f"timeout must be non-negative, got {v}")
        return v
    if isinstance(v, float):
        if v < 0:
            raise ValueError(f"timeout must be non-negative, got {v}")
        return int(v)
    if isinstance(v, str):
        m = _TIMEOUT_RE.match(v)
        if not m:
            raise ValueError(f"invalid timeout string: {v!r}")
        value = float(m.group(1))
        unit = (m.group(2) or "s").lower()
        if unit not in _TIMEOUT_UNITS:
            raise ValueError(f"unknown timeout unit {unit!r} in {v!r}")
        return int(value * _TIMEOUT_UNITS[unit])
    raise TypeError(f"timeout must be int, float, or str; got {type(v).__name__}")


TimeoutSeconds = Annotated[int, BeforeValidator(_coerce_timeout)]


# ---- YAML settings source ---------------------------------------------------


class YamlSettingsSource(PydanticBaseSettingsSource):
    """Load ``config.yaml`` from disk as a settings source.

    Returns an empty dict when the file is missing or empty so that other
    sources (env, defaults) still apply. The path is taken from
    ``init_kwargs['config_yaml_path']`` so tests can isolate their YAML file.
    """

    def __init__(
        self,
        settings_class: type[BaseSettings],
        init_kwargs: Mapping[str, Any] | None = None,
        default_yaml_path: Path | str = "config.yaml",
    ) -> None:
        super().__init__(settings_class)
        configured = (init_kwargs or {}).get("config_yaml_path")
        # Fall back to ``PROXY_CONFIG_YAML`` env var when no
        # ``config_yaml_path`` is passed in code. Used by the
        # container-mode proxy spawn (which can't pass init args
        # because the container process starts from a fixed
        # uvicorn command line) — the desktop GUI writes the
        # settings to ``/etc/sharkdrown/config.yaml`` and sets
        # this env var so the proxy picks them up.
        if not configured:
            import os

            env_path = os.environ.get("PROXY_CONFIG_YAML")
            if env_path:
                configured = env_path
        self.yaml_file = Path(configured) if configured else Path(default_yaml_path)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # All work happens in __call__; nothing per-field to report here.
        return None, field_name, False

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
    ) -> Any:
        return value

    def __call__(self) -> dict[str, Any]:
        path = Path(self.yaml_file)
        if not path.is_file():
            return {}
        try:
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
            # Log which file we loaded and what shape it has.
            # ``model_endpoints`` is the only block the desktop
            # populates dynamically; the others come from YAML
            # or env. Logging the endpoint count is enough to
            # confirm the per-profile routing config made it
            # into the container (the most common cause of a
            # silent routing-to-default-anthropic failure is a
            # stale config.yaml or a missing volume mount).
            model_endpoints = (
                data.get("model_endpoints") if isinstance(data, dict) else None
            ) or {}
            logger.info(
                "loaded proxy config from %s: %d endpoint(s), %d alias(es), %d provider(s)",
                path,
                len(model_endpoints.get("endpoints") or {}),
                len(model_endpoints.get("model_aliases") or {}),
                len(model_endpoints.get("providers") or {}),
            )
        except yaml.YAMLError:
            logger.warning("config.yaml at %s is invalid; ignoring", path, exc_info=True)
            return {}
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            logger.warning("config.yaml at %s is not a mapping; ignoring", path)
            return {}
        return dict(data)


# ---- Settings hierarchy -----------------------------------------------------


class Timeouts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    read: TimeoutSeconds = 600
    write: TimeoutSeconds = 600
    idle: TimeoutSeconds = 600


class AnthropicProviderCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: HttpUrl = HttpUrl("https://api.anthropic.com")
    version: str = "2023-06-01"
    max_retries: int = 3


class OpenAIProviderCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: HttpUrl = HttpUrl("https://api.openai.com")
    api_key: SecretStr = SecretStr("")


class ProvidersCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    anthropic: AnthropicProviderCfg = AnthropicProviderCfg()
    openai: OpenAIProviderCfg = OpenAIProviderCfg()


class StorageCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    db_path: Path = Path("requests.db")
    requests_dir: Path | None = None


class SubagentsCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    mappings: dict[str, str] = Field(default_factory=dict)


class ServerCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    port: int = 3001
    timeouts: Timeouts = Timeouts()


class ProviderCfg(BaseModel):
    """One upstream provider — name → URL + plaintext api_key + catalog.

    Populated by the embedded desktop app from the user's ``Provider``
    record. The ``api_key`` is the *resolved* plaintext value
    (CredentialStore ref materialised at proxy boot) — this avoids
    carrying ref-name strings across threads.

    ``available_models`` is a read-only catalog the proxy exposes via
    ``GET /v1/models`` (future work) and uses internally to validate
    that a routed request names a model the provider actually hosts.

    ``extra="forbid"`` catches YAML typos at startup. Safe here because
    the desktop app is the sole writer and always uses known field names.
    """

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    api_key: SecretStr | None = None
    available_models: list[str] = Field(default_factory=list)


class ModelEndpointsCfg(BaseModel):
    """Per-model upstream endpoint + provider overrides.

    Resolution order (in
    :func:`ccproxy.app.api.proxy._resolve_request_target`):

    1. ``endpoints[model]`` → ``provider_name``
    2. ``providers[provider_name]`` → ``(url, api_key)``

    If a model isn't in ``endpoints`` we fall back to
    ``default_provider``. ``default_endpoint`` is a final backstop
    URL (used only when *neither* mapping resolves, for legacy
    configs that didn't migrate to the providers dict).

    ``model_aliases`` is a flat ``alias → real_model_name`` map. The
    proxy rewrites an incoming ``req.model`` through this map before
    routing / URL resolution runs, so a client can pass ``"opus"``
    and the proxy forwards the request under ``"claude-opus-4-8"``.

    Populated by the embedded desktop app from the active profile.
    """

    model_config = ConfigDict(extra="ignore")

    default_provider: str | None = None
    providers: dict[str, ProviderCfg] = Field(default_factory=dict)
    endpoints: dict[str, str] = Field(default_factory=dict)
    # ``endpoints`` is ``model_name → provider_name`` in the new
    # shape (the value used to be a URL — older reads of legacy
    # YAML may still surface strings that look like URLs; the
    # resolver rejects them with a clear failure mode).
    default_endpoint: str | None = None
    model_aliases: dict[str, str] = Field(default_factory=dict)


class Settings(BaseSettings):
    """Root configuration.

    Precedence (high → low): init args → env vars → ``config.yaml`` → ``.env`` → defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Top-level env aliases (flat) used to override nested YAML fields.
        env_prefix="",
    )

    server: ServerCfg = ServerCfg()
    providers: ProvidersCfg = ProvidersCfg()
    storage: StorageCfg = StorageCfg()
    subagents: SubagentsCfg = SubagentsCfg()
    model_endpoints: ModelEndpointsCfg = ModelEndpointsCfg()

    # ---- env overrides (flat) ----
    port: int | None = None
    read_timeout: int | None = None
    write_timeout: int | None = None
    idle_timeout: int | None = None
    anthropic_forward_url: str | None = None
    anthropic_version: str | None = None
    anthropic_max_retries: int | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    db_path: str | None = None
    subagent_mappings: str | None = None  # "name:model,name:model" CSV

    # Used by the YAML source to locate the file (overridable for tests).
    config_yaml_path: Path | None = Field(default=None, exclude=True)

    # ---- customise source order: init > env > yaml > dotenv ----
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The init source's init_kwargs carry the optional ``config_yaml_path``.
        init_kwargs = getattr(init_settings, "init_kwargs", None) or {}
        return (
            init_settings,
            env_settings,
            YamlSettingsSource(settings_cls, init_kwargs=init_kwargs),
            dotenv_settings,
            file_secret_settings,
        )

    # ---- validators ----
    @field_validator("anthropic_forward_url", "openai_base_url", "db_path")
    @classmethod
    def _strip_str(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v2 = v.strip()
        return v2 or None

    @field_validator("anthropic_max_retries")
    @classmethod
    def _clamp_max_retries(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if v < 0:
            raise ValueError("anthropic_max_retries must be >= 0")
        return v

    @model_validator(mode="before")
    @classmethod
    def _parse_subagent_csv(cls, data: Any) -> Any:
        """Parse ``SUBAGENT_MAPPINGS="name:model,name:model"`` from any source.

        Runs in ``before`` mode so it sees both dict inputs (from YAML/init) and
        flat env-var dicts. The CSV is merged into ``subagents.mappings``.
        """
        if not isinstance(data, dict):
            return data
        csv = data.get("subagent_mappings")
        if csv is None:
            return data
        parsed: dict[str, str] = {}
        if isinstance(csv, str):
            for chunk in csv.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if ":" not in chunk:
                    raise ValueError(f"subagent_mappings chunk {chunk!r} must be 'name:model'")
                name, _, model = chunk.partition(":")
                name = name.strip()
                model = model.strip()
                if not name or not model:
                    raise ValueError(f"subagent_mappings chunk {chunk!r} must be 'name:model'")
                parsed[name] = model
        elif isinstance(csv, Mapping):
            parsed = {str(k): str(v) for k, v in csv.items()}
        else:
            raise ValueError(
                f"subagent_mappings must be a CSV string or a mapping, got {type(csv).__name__}"
            )

        # Merge into subagents.mappings.
        sub = data.get("subagents")
        if sub is None:
            sub = {}
        if not isinstance(sub, dict):
            raise ValueError("subagents must be a mapping")
        existing = sub.get("mappings") or {}
        if not isinstance(existing, dict):
            raise ValueError("subagents.mappings must be a mapping")
        sub["mappings"] = {**existing, **parsed}
        # Providing mappings implies subagent routing is wanted. This is a
        # direct assignment (not ``setdefault``) because a YAML/dotenv value
        # of ``enable: false`` would otherwise silently win — surprising and
        # exactly the footgun we hit when the test default config shipped
        # with ``enable: false``.
        if parsed:
            sub["enable"] = True
        data["subagents"] = sub
        return data

    @model_validator(mode="after")
    def _apply_env_overrides(self) -> Settings:
        """Project flat env fields into the nested config tree."""
        if self.port is not None:
            self.server.port = self.port
        if self.read_timeout is not None:
            self.server.timeouts.read = self.read_timeout
        if self.write_timeout is not None:
            self.server.timeouts.write = self.write_timeout
        if self.idle_timeout is not None:
            self.server.timeouts.idle = self.idle_timeout
        if self.anthropic_forward_url is not None:
            self.providers.anthropic.base_url = HttpUrl(self.anthropic_forward_url)
        if self.anthropic_version is not None:
            self.providers.anthropic.version = self.anthropic_version
        if self.anthropic_max_retries is not None:
            self.providers.anthropic.max_retries = self.anthropic_max_retries
        if self.openai_api_key is not None:
            self.providers.openai.api_key = self.openai_api_key
        if self.openai_base_url is not None:
            self.providers.openai.base_url = HttpUrl(self.openai_base_url)
        if self.db_path is not None:
            self.storage.db_path = Path(self.db_path)
        return self
