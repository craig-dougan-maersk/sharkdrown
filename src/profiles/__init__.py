"""Claude Code session profiles — model endpoints, credentials, env, settings.

Public surface
--------------
- :class:`ClaudeProfile` — one profile's worth of configuration
- :class:`ModelEndpoint` — model → named :class:`Provider` with an
  optional credential ref
- :class:`Provider` — a named upstream service (e.g. "Anthropic",
  "MiniMax") and its base URL
- :class:`Subagent` — one ``agents/<name>.md`` entry's worth of frontmatter
- :class:`MarketplaceSource` — one Claude Code plugin marketplace
  (matches ``known_marketplaces.json[name]`` shape)
- :class:`InstalledPlugin` — one entry in the per-profile installed
  plugins registry (``installed_plugins.json``)
- :class:`McpServer` — one entry in ``.mcp.json``'s ``mcpServers`` map
- :func:`slugify` — derive a filesystem-safe slug from a profile name
- :class:`CredentialStore` — read/write API keys (chmod 600 JSON)
- :class:`ProfileStore` — read/write profiles (chmod 600 JSON) +
  migrate-from-ProxyConfig-on-first-launch
- :func:`write_profile_config` — render ``settings.json`` + ``agents/`` to disk
- :func:`build_claude_session_env` — assemble the env dict for a session
"""

from src.profiles.config_dir import write_profile_config
from src.profiles.credential_store import CredentialStore
from src.profiles.env_builder import (
    INFRA_ENV_VARS,
    build_claude_session_env,
    resolve_api_key,
)
from src.profiles.models import (
    ClaudeProfile,
    InstalledPlugin,
    MarketplaceSource,
    McpServer,
    ModelEndpoint,
    Provider,
    Subagent,
    slugify,
)
from src.profiles.store import (
    DEFAULT_PROFILE_NAME,
    SCHEMA_VERSION,
    ProfileStore,
    SENTINEL_API_KEY,
)

__all__ = [
    "ClaudeProfile",
    "CredentialStore",
    "DEFAULT_PROFILE_NAME",
    "INFRA_ENV_VARS",
    "InstalledPlugin",
    "MarketplaceSource",
    "McpServer",
    "ModelEndpoint",
    "ProfileStore",
    "Provider",
    "SCHEMA_VERSION",
    "SENTINEL_API_KEY",
    "Subagent",
    "build_claude_session_env",
    "resolve_api_key",
    "slugify",
    "write_profile_config",
]