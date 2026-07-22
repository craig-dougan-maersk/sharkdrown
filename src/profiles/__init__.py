"""Claude Code session profiles — model endpoints, credentials, env, settings.

Public surface
--------------
- :class:`ClaudeProfile` — one profile's worth of configuration
- :class:`ModelEndpoint` — upstream URL for a single model (per-model
  credentials are deferred to Phase 3; v1 shares one key per profile)
- :class:`Subagent` — one ``agents/<name>.md`` entry's worth of frontmatter
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
)
from src.profiles.models import (
    ClaudeProfile,
    ModelEndpoint,
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
    "ModelEndpoint",
    "ProfileStore",
    "SCHEMA_VERSION",
    "SENTINEL_API_KEY",
    "Subagent",
    "build_claude_session_env",
    "slugify",
    "write_profile_config",
]