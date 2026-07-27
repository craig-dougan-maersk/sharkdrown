# Configuration Guide

## Overview

SharkDrown has a layered configuration system spanning profiles, credentials, proxy settings, and environment variables. Configuration flows from multiple sources with defined precedence rules.

## Profile System

### Profile JSON Schema

Profiles are stored in `<data_dir>/profiles.json` (chmod 600):

```json
{
  "active_index": 0,
  "profiles": [
    {
      "name": "Default",
      "slug": "default",
      "providers": {
        "Anthropic": {
          "name": "Anthropic",
          "url": "https://api.anthropic.com",
          "key_ref": "default-key",
          "available_models": ["claude-opus-4-8", "claude-sonnet-4-6"]
        }
      },
      "model_endpoints": {
        "claude-opus-4-8": {
          "provider_name": "Anthropic",
          "aliases": ["opus"]
        }
      },
      "default_model": "opus",
      "env_vars": {},
      "claude_settings": {},
      "subagents": [],
      "session_cwd": null,
      "bind_address": "127.0.0.1",
      "port": 3001,
      "config_dir": "",
      "is_default": true
    }
  ]
}
```

### Data Directory Locations

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/Sharkdrown/PandasMarkdownEditor/` |
| Linux | `~/.local/share/Sharkdrown/PandasMarkdownEditor/` |
| Windows | `%APPDATA%/Sharkdrown/PandasMarkdownEditor/` |

### Key Files in Data Directory

| File | Purpose |
|------|---------|
| `profiles.json` | All profiles (chmod 600) |
| `credentials.json` | API keys (chmod 600) |
| `proxy.db` | SQLite request capture |
| `claude_profiles/<slug>/settings.json` | Per-profile CLAUDE_CONFIG_DIR |
| `claude_profiles/<slug>/agents/*.md` | Per-profile subagent definitions |

---

## Credential Store

API keys are stored separately from profiles in `credentials.json`:

```json
{
  "default-key": "sk-ant-api03-...",
  "work-openai": "sk-proj-..."
}
```

### Mechanics

- Keys are referenced by stable `ref` strings (e.g., `"default-key"`)
- Profiles reference keys via `Provider.key_ref` — changing a key propagates to all providers using that ref
- Atomic writes: write to `.tmp`, `chmod 600`, `os.replace()`
- Keys are whitespace-stripped on save (handles copy-paste with newlines)
- File permissions: `0o600` (owner read/write only) enforced on every write

### Security Model

- Plaintext JSON with POSIX file permissions
- System keyring intentionally avoided (WSL2/headless Linux incompatibility)
- Keys never appear in `profiles.json` — credential store is the single source
- Proxy sanitizes keys in captured headers (SHA-256 hash stored, not plaintext)

---

## Proxy Configuration Layering

The embedded proxy uses pydantic-settings with multiple sources (highest precedence first):

```
1. Init args (passed to Settings(...) from profile overrides)
2. Environment variables
3. config.yaml (YAML settings source)
4. .env file (pydantic-settings dotenv)
5. Field defaults (hardcoded in Settings class)
```

### Config Discovery Search Order

When `config_dir` is not explicitly set, the proxy searches for `config.yaml` and `.env`:

1. Application data directory
2. Current working directory
3. Known parent directories (original source-proxy location)

The first file found wins. `find_config_yaml(explicit_dir)` checks the explicit dir first if provided.

### config.yaml Structure

```yaml
server:
  host: "127.0.0.1"
  port: 3001
  timeout: "5m"           # Duration strings supported: "10s", "5m", "1h"

providers:
  anthropic:
    url: "https://api.anthropic.com"
  openai:
    url: "https://api.openai.com"
    api_key: "sk-..."     # Or use OPENAI_API_KEY env var

storage:
  path: "./proxy.db"
  retention_days: 30

subagents:
  enable: true
  mappings: "code-reviewer:gpt-4o,explorer:claude-sonnet-4-6"

model_endpoints:
  default_endpoint: "anthropic"
  endpoints:
    claude-opus-4-8: "anthropic"
    gpt-4o: "openai"
  model_aliases:
    opus: "claude-opus-4-8"
    sonnet: "claude-sonnet-4-6"
```

### Pydantic Settings Hierarchy

```
Settings
├── server: ServerCfg (host, port, timeout)
├── providers: ProvidersCfg (anthropic, openai, custom providers)
├── storage: StorageCfg (path, retention)
├── subagents: SubagentsCfg (enable, mappings CSV)
└── model_endpoints: ModelEndpointsCfg
    ├── default_endpoint: str
    ├── endpoints: dict[model, provider_name]
    ├── providers: dict[name, ProviderCfg]
    └── model_aliases: dict[alias, real_model]
```

---

## Environment Variable System

### Infrastructure Variables (Safety Net)

These variables are forcibly applied by `build_claude_session_env()` and cannot be overridden by profile settings:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_BASE_URL` | Points Claude Code at the proxy (e.g., `http://127.0.0.1:3001`) |
| `CLAUDE_CONFIG_DIR` | Per-profile config directory with settings.json + agents/ |
| `ANTHROPIC_API_KEY` | Resolved from credential store (or sentinel if unconfigured) |
| `ANTHROPIC_MODEL` | Default model from profile |
| `ANTHROPIC_AUTH_TOKEN` | Auth token if applicable |
| `TERM` | Terminal type (default: `xterm-256color`) |
| `COLORTERM` | Color support (default: `truecolor`) |

If a profile's `env_vars` attempts to override any of these, a `UserWarning` is emitted but the infrastructure value wins.

### Environment Layering Precedence

```
Final env = os.environ
          + infra vars (ANTHROPIC_BASE_URL, CLAUDE_CONFIG_DIR, etc.)
          + profile.env_vars
          + session_env (per-launch overrides)
          + safety net (re-apply infra vars on top)
```

### Sentinel API Key

When no real API key is configured:
- `ANTHROPIC_API_KEY` is set to `sk-proxy-injected`
- Claude Code launches without auth errors
- The proxy rejects upstream calls until a real key is configured

### Environment Variable Catalog

The profile editor includes a curated catalog of ~60 Claude Code environment variables organized by section:

- **Authentication**: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`
- **Model Selection**: `ANTHROPIC_MODEL`, `CLAUDE_CODE_MAX_TURNS`
- **Timeouts**: `CLAUDE_CODE_MAX_THINKING_TOKENS`, `CLAUDE_CODE_TIMEOUT_MS`
- **Session**: `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`
- **Compaction**: `CLAUDE_CODE_COMPRESS_CONTEXT_*`
- **Background Tasks**: `CLAUDE_CODE_BACKGROUND_*`
- **UI**: `CLAUDE_CODE_THEME`, `NO_COLOR`

---

## Per-Profile CLAUDE_CONFIG_DIR

Each profile gets its own config directory at `<data_dir>/claude_profiles/<slug>/`:

### Generated settings.json

```json
{
  "availableModels": ["opus", "sonnet", "fast"],
  "model": "opus",
  "permissions": { ... },
  "hooks": { ... }
}
```

Notes:
- `availableModels` contains **only aliases** (never real model IDs)
- `model` is the profile's `default_model`
- Additional keys from `profile.claude_settings` are merged (derived keys win on conflict)

### Generated agents/*.md

```markdown
---
name: code-reviewer
description: Reviews code for quality and correctness
tools:
  - Read
  - Bash
model: gpt-4o
---

You are a code reviewer. Focus on...
```

- Rendered from `profile.subagents` list
- YAML frontmatter + markdown body (system prompt)
- Stale files are pruned on each profile save

---

## Legacy Migration

On first launch, if a `ProxyConfig` (QSettings-based) exists but no `profiles.json`, the profile store performs automatic migration:

1. Reads legacy QSettings keys (host, port, model_endpoints, default_endpoint)
2. Creates a "Default" profile with migrated values
3. Writes `profiles.json`
4. Legacy QSettings remain untouched (non-destructive)
