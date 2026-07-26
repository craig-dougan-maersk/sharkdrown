"""Curated catalog of Claude Code / Anthropic environment variables.

The ``Add Standard…`` picker in :class:`ProfileEditorDialog` reads
from this catalog to populate its two-column (variable, purpose)
list. Variables and their descriptions are taken from the public
Claude Code env-var reference at https://code.claude.com/docs/en/env-vars
and grouped so the picker can section them for the user.

Why a separate module
---------------------
The catalog is *data*, not UI; keeping it here means:

* The picker dialog (``src/ui/claude/profile_editor_dialog.py``)
  stays focused on layout and signal wiring.
* The catalog is trivially unit-testable as plain Python — no
  QApplication needed.
* Variables can be referenced from other code (e.g. ``INFRA_ENV_VARS``
  in ``env_builder.py``) without re-deriving them from the doc page.

Forbidden variables
-------------------
Three variables are infrastructure-owned and *cannot* be set via
this picker — the Claude integration overrides them per session:

* ``ANTHROPIC_API_KEY`` — set via the API Key field (CredentialStore)
* ``ANTHROPIC_BASE_URL`` — derived from the active profile's bind/port
* ``CLAUDE_CONFIG_DIR`` — derived from ``write_profile_config``

They are listed in ``INFRA_ENV_VARS`` and excluded from the picker
silently — the user shouldn't be tempted to set them, since the
integration will clobber them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StandardEnvVar:
    """One curated env var entry.

    Attributes
    ----------
    name
        The env var name as it appears in ``os.environ`` (e.g.
        ``"ANTHROPIC_MODEL"``). All-caps with underscores.
    purpose
        One-line description from the env-vars reference page. Shown
        verbatim in the picker's right column.
    section
        Group label used by the picker to visually separate concerns
        (e.g. ``"Authentication"``, ``"Model Selection"``).
    """

    name: str
    purpose: str
    section: str


#: Variables the integration owns and overrides per session.
#: Mirrors the constant in ``env_builder.py`` — duplicated here so
#: the catalog stays self-contained for testing.
INFRA_OWNED_VARS: frozenset[str] = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"}
)


#: All curated standard env vars, ordered roughly as they appear on
#: the public reference page. Frozen tuples preserve insertion order
#: so the picker can render top-to-bottom without sorting.
_STANDARD_VARS: tuple[StandardEnvVar, ...] = (
    # ---- Anthropic API & Authentication ----
    StandardEnvVar(
        "ANTHROPIC_AUTH_TOKEN",
        "Custom value for Authorization header (prefixed with Bearer )",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_AWS_API_KEY",
        "Workspace API key for Claude Platform on AWS",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_AWS_BASE_URL",
        "Override Claude Platform on AWS endpoint URL",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_AWS_WORKSPACE_ID",
        "Workspace ID sent as anthropic-workspace-id header",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_BEDROCK_BASE_URL",
        "Override Amazon Bedrock endpoint URL",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "Override Amazon Bedrock Mantle endpoint URL",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_BEDROCK_SERVICE_TIER",
        "Bedrock service tier (default, flex, priority)",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_BETAS",
        "Comma-separated list of additional anthropic-beta header values",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_CUSTOM_HEADERS",
        "Custom headers (Name: Value format, newline-separated)",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_FOUNDRY_API_KEY",
        "API key for Microsoft Foundry authentication",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        "Bearer token for Microsoft Foundry auth (Entra token)",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "Full base URL for Microsoft Foundry resource",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "Microsoft Foundry resource name",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_VERTEX_BASE_URL",
        "Override Google Cloud's Agent Platform endpoint URL",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GCP project ID for Google Cloud's Agent Platform",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "ANTHROPIC_WORKSPACE_ID",
        "Workspace ID for workload identity federation",
        "Anthropic API & Authentication",
    ),
    StandardEnvVar(
        "AWS_BEARER_TOKEN_BEDROCK",
        "Amazon Bedrock API key for authentication",
        "Anthropic API & Authentication",
    ),
    # ---- Model Selection ----
    StandardEnvVar(
        "ANTHROPIC_MODEL",
        "Name of the model setting to use",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_SMALL_FAST_MODEL",
        "[DEPRECATED] Haiku-class model for background tasks",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
        "Override AWS region for Haiku-class model on Bedrock",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "Model ID to add as custom entry in /model picker",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
        "Display description for custom model",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
        "Display name for custom model entry",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES",
        "Custom model supported capabilities",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "Default Haiku model",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
        "Default Haiku model description",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
        "Default Haiku model display name",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
        "Default Haiku model capabilities",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "Default Opus model",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
        "Default Opus model description",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "Default Opus model display name",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
        "Default Opus model capabilities",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "Default Sonnet model",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION",
        "Default Sonnet model description",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
        "Default Sonnet model display name",
        "Model Selection",
    ),
    StandardEnvVar(
        "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES",
        "Default Sonnet model capabilities",
        "Model Selection",
    ),
    # ---- Timeouts & Network ----
    StandardEnvVar(
        "API_TIMEOUT_MS",
        "Timeout for API requests in ms (default: 600000; max: 2147483647)",
        "Timeouts & Network",
    ),
    StandardEnvVar(
        "API_FORCE_IDLE_TIMEOUT",
        "Override 5-min idle timeout on streaming responses",
        "Timeouts & Network",
    ),
    StandardEnvVar(
        "BASH_DEFAULT_TIMEOUT_MS",
        "Default timeout for long-running bash commands (default: 120000)",
        "Timeouts & Network",
    ),
    StandardEnvVar(
        "BASH_MAX_OUTPUT_LENGTH",
        "Max bash output chars before saving to file",
        "Timeouts & Network",
    ),
    StandardEnvVar(
        "BASH_MAX_TIMEOUT_MS",
        "Maximum timeout model can set for bash (default: 600000)",
        "Timeouts & Network",
    ),
    # ---- Session & Subprocess ----
    StandardEnvVar(
        "CLAUDECODE",
        "Set to 1 in Claude Code subprocesses to detect nested execution",
        "Session & Subprocess",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_CHILD_SESSION",
        "Set to 1 in subprocesses to distinguish nested sessions",
        "Session & Subprocess",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "Session ID set during active Remote Control connection",
        "Session & Subprocess",
    ),
    StandardEnvVar(
        "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
        "Return to original cwd after each Bash/PowerShell command",
        "Session & Subprocess",
    ),
    # ---- Compaction & Context ----
    StandardEnvVar(
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "Context capacity in tokens for auto-compaction calculations",
        "Compaction & Context",
    ),
    StandardEnvVar(
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        "Percentage (1-100) at which auto-compaction triggers",
        "Compaction & Context",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_1M_CONTEXT",
        "Set to 1 to disable 1M context window support",
        "Compaction & Context",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
        "Disable adaptive reasoning on Opus 4.6/Sonnet 4.6",
        "Compaction & Context",
    ),
    # ---- Background Tasks & Agents ----
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
        "Disable all background task functionality",
        "Background Tasks & Agents",
    ),
    StandardEnvVar(
        "CLAUDE_AUTO_BACKGROUND_TASKS",
        "Force-enable automatic backgrounding of long-running tasks",
        "Background Tasks & Agents",
    ),
    StandardEnvVar(
        "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS",
        "Stall timeout for background subagents (default: 600000)",
        "Background Tasks & Agents",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_AGENT_VIEW",
        "Turn off background agents and agent view",
        "Background Tasks & Agents",
    ),
    StandardEnvVar(
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS",
        "Disable built-in subagent types (non-interactive mode)",
        "Background Tasks & Agents",
    ),
    # ---- UI & Display ----
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN",
        "Disable fullscreen rendering, use classic renderer",
        "UI & Display",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
        "Repaint entire screen each frame in fullscreen mode",
        "UI & Display",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_NO_FLICKER",
        "Flicker-prevention setting for fullscreen mode",
        "UI & Display",
    ),
    StandardEnvVar(
        "CLAUDE_AX_SCREEN_READER",
        "Render screen-reader friendly output",
        "UI & Display",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_ACCESSIBILITY",
        "Keep native cursor visible for screen magnifiers",
        "UI & Display",
    ),
    # ---- Auto-continue & Dialogs ----
    StandardEnvVar(
        "CLAUDE_AFK_TIMEOUT_MS",
        "Idle ms before unanswered AskUserQuestion auto-continues",
        "Auto-continue & Dialogs",
    ),
    StandardEnvVar(
        "CLAUDE_AFK_COUNTDOWN_MS",
        "Ms before on-screen countdown appears (default: 20000)",
        "Auto-continue & Dialogs",
    ),
    # ---- Artifacts & Memory ----
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_ARTIFACT",
        "Disable the Artifact tool",
        "Artifacts & Memory",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_ARTIFACT_AUTO_OPEN",
        "Stop auto-opening browser for new artifacts",
        "Artifacts & Memory",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "Disable auto memory creation/loading",
        "Artifacts & Memory",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
        "Load memory files from --add-dir directories",
        "Artifacts & Memory",
    ),
    # ---- Debug & Logging ----
    StandardEnvVar(
        "CLAUDE_CODE_DEBUG_LOGS_DIR",
        "Override debug log file path",
        "Debug & Logging",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DEBUG_LOG_LEVEL",
        "Min log level (verbose, debug, info, warn, error)",
        "Debug & Logging",
    ),
    # ---- Cloud & Remote ----
    StandardEnvVar(
        "CCR_FORCE_BUNDLE",
        "Force claude --cloud to bundle/upload local repo",
        "Cloud & Remote",
    ),
    StandardEnvVar(
        "CLAUDE_CLIENT_PRESENCE_FILE",
        "Path to file indicating screen-lock state for push notifications",
        "Cloud & Remote",
    ),
    # ---- AWS & Auth Helpers ----
    StandardEnvVar(
        "CLAUDE_CODE_AWS_CHAIN_RESOLVE_TIMEOUT_MS",
        "Wait time for AWS default credential chain (default: 60000)",
        "AWS & Auth Helpers",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "Credential refresh interval for apiKeyHelper",
        "AWS & Auth Helpers",
    ),
    # ---- TLS & Networking ----
    StandardEnvVar(
        "CLAUDE_CODE_CERT_STORE",
        "CA cert sources (bundled, system); default bundled,system",
        "TLS & Networking",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_CLIENT_CERT",
        "Path to client cert for mTLS",
        "TLS & Networking",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_CLIENT_KEY",
        "Path to client private key for mTLS",
        "TLS & Networking",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
        "Passphrase for encrypted client key",
        "TLS & Networking",
    ),
    # ---- Feature Toggles ----
    StandardEnvVar(
        "CLAUDE_CODE_AUTO_CONNECT_IDE",
        "Override automatic IDE connection",
        "Feature Toggles",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
        "Send effort parameter with every request",
        "Feature Toggles",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_ATTRIBUTION_HEADER",
        "Set to 0 to omit attribution block from system prompt",
        "Feature Toggles",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
        "Disable the advisor tool",
        "Feature Toggles",
    ),
    StandardEnvVar(
        "CLAUDE_CODE_DISABLE_ATTACHMENTS",
        "Disable attachment processing (@ syntax)",
        "Feature Toggles",
    ),
    # ---- Status Line & Persistence ----
    StandardEnvVar(
        "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE",
        "Override exclusion of nested sessions from persistence",
        "Status Line & Persistence",
    ),
)


def standard_env_vars() -> tuple[StandardEnvVar, ...]:
    """Return all curated env vars, excluding infrastructure-owned ones.

    The integration manages ``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``
    and ``CLAUDE_CONFIG_DIR`` per session — they are removed from this
    catalog so the user isn't tempted to set values the system will
    override anyway.
    """
    return tuple(v for v in _STANDARD_VARS if v.name not in INFRA_OWNED_VARS)


def standard_env_var_names() -> frozenset[str]:
    """Convenience: names from :func:`standard_env_vars` as a set."""
    return frozenset(v.name for v in standard_env_vars())


def sections() -> tuple[str, ...]:
    """Ordered list of distinct section labels, preserving appearance order.

    The picker uses this to render section headers. Sections with no
    variables after the infra-filter are dropped.
    """
    seen: dict[str, None] = {}
    for v in standard_env_vars():
        seen.setdefault(v.section, None)
    return tuple(seen.keys())