# Configuration Guide

This document enumerates every file SharkDrown v3 reads or writes, where it
lives on disk, and which code paths touch it. If you can't find what you're
looking for here, grep for the filename in `src/` — every file mentioned
below has at least one reader and one writer linked.

## Where everything lives

The app stores all of its state under
[`QStandardPaths.AppDataLocation`](https://doc.qt.io/qt-6/qstandardpaths.html)
which resolves to:

| Platform | Path |
|----------|------|
| macOS | `~/Library/Application Support/SharkDrown/SharkDrown/` |
| Linux | `~/.local/share/SharkDrown/SharkDrown/` |
| Windows | `%APPDATA%/SharkDrown/SharkDrown/` |

Substitute `<data_dir>` for the path above. The runtime code resolves this via
[`src/proxy/paths.py:data_dir()`](src/proxy/paths.py) (which requires
`setOrganizationName("SharkDrown")` and `setApplicationName("SharkDrown")` to
have been called on the `QApplication` first — `src/main.py` does this on
startup).

User-level files (logs, recordings) live under `~/.sharkdrown/` regardless of
the app-data location; see [§ User-level files](#user-level-files).

## At a glance

```
<data_dir>/
├── profiles.json                     # Claude profile registry (chmod 600)
├── credentials.json                  # API keys, keyed by stable refs (chmod 600)
├── claude_global.json                # Global Claude settings (chmod 600)
├── defaults.env                      # User-editable global env vars (chmod 600)
├── ccproxy-requests.db               # SQLite request capture
└── claude_profiles/
    └── <slug>/
        └── .claude/                  # bind-mounted to /home/claude/.claude
            ├── settings.json         # Claude Code's CLAUDE_CONFIG_DIR/settings.json
            ├── .mcp.json             # MCP server registry
            ├── CLAUDE.md             # Global CLAUDE.md prepended + profile body
            ├── agents/
            │   └── <slug>.md         # Subagent (YAML frontmatter + body)
            └── plugins/
                ├── installed_plugins.json
                ├── known_marketplaces.json
                └── blocklist.json

~/.sharkdrown/
├── logs/
│   └── <label>_<epoch>.log           # Plain-text session log (toggleable)
└── recordings/
    └── session_<epoch>.cast          # asciicast v2 recording

OS-native: registry / plist / INI
└── "SharkDrown" / "App"              # QSettings — UI state, theme, etc.
```

## Data-directory files

### `profiles.json`

The Claude profile registry. Holds every profile the user has created plus
which one is currently active. Written by the profile editor and the
profile-management UI; read by the proxy lifecycle, session launcher, and
profile menu.

| | |
|---|---|
| **Path** | `<data_dir>/profiles.json` |
| **Permissions** | `0o600` (POSIX); per-user on Windows |
| **Format** | JSON, atomic write via `tempfile.mkstemp` + `os.replace` |
| **Reader** | `src/profiles/store.py:ProfileStore.load` |
| **Writer** | `src/profiles/store.py:ProfileStore.save` |
| **Schema version** | `schema_version: 1` |

Top-level shape:

```json
{
  "schema_version": 1,
  "active_index": 0,
  "profiles": [
    {
      "name": "Default",
      "slug": "default",
      "providers": { "Anthropic": { "name": "Anthropic", "url": "...", "key_ref": "...", "available_models": [...] } },
      "model_endpoints": { "claude-opus-4-8": { "provider_name": "Anthropic", "aliases": ["opus"] } },
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

On first launch (no `profiles.json`), SharkDrown performs a one-time
migration from the legacy `QSettings`-based `ProxyConfig`. See
[§ Legacy migration](#legacy-migration).

### `credentials.json`

API keys referenced by `Provider.key_ref`. Keys are NEVER embedded in
`profiles.json` — the credential store is the single source of truth so
rotating one key propagates to every provider that references it.

| | |
|---|---|
| **Path** | `<data_dir>/credentials.json` |
| **Permissions** | `0o600` |
| **Format** | JSON `{ "<ref>": "<secret>" }`, atomic write |
| **Reader** | `src/profiles/credential_store.py:CredentialStore.get` |
| **Writer** | `src/profiles/credential_store.py:CredentialStore.set` |
| **Keyring fallback** | When `pip install -e ".[keyring]"` is used and a system keyring is available, the value written to JSON is the sentinel `<keyring>` and the actual secret lives in the keyring. `get()` resolves sentinels transparently. |

### `claude_global.json`

Truly-global Claude Code settings shared by every profile. Most fields were
promoted from the per-profile General tab during the menu refactor.

| | |
|---|---|
| **Path** | `<data_dir>/claude_global.json` |
| **Permissions** | `0o600` |
| **Format** | JSON, atomic write |
| **Reader** | `src/profiles/global_settings.py:GlobalSettingsStore.load` |
| **Writer** | `src/ui/claude/global_settings/dialog.py` (on dialog accept) |

Fields:

| Key | Default | Purpose |
|-----|---------|---------|
| `bind_address` | `"127.0.0.1"` | Proxy bind address; `0.0.0.0` for LAN / Docker exposure |
| `port` | `3001` | Proxy TCP port; `0` = OS picks a free port |
| `config_dir` | `""` | Where the proxy reads `config.yaml` / `.env`; empty = auto-discover |
| `session_cwd` | `null` | Default working directory for Claude Code sessions |
| `providers` | `{}` | Named provider registry shared by every profile |
| `container_runtime` | `"docker"` | Path to container binary (`"podman"`, or absolute path) |
| `default_claude_image` | `"claude-code:latest"` | Image used when a profile's `container_image` is empty |
| `proxy_image` | `"sharkdrown-proxy:latest"` | Image used when `run_proxy_in_container` is true |
| `run_proxy_in_container` | `false` | Run the proxy in a container instead of the local QThread |
| `claude_image_auto_rebuild` | `true` | Rebuild the Claude image with `--build-arg CLAUDE_UID=$(id -u)` if the cached image's UID doesn't match the host |
| `tui_mode` | `"fullscreen"` | `"fullscreen"` keeps `CLAUDE_CODE_NO_FLICKER=1`; `"default"` or `"off"` drops it so Claude Code uses its own non-fullscreen mode. See [§ Bug A: /tui mode defaulting to fullscreen](#bug-a-tui-mode-defaulting-to-fullscreen) |

### `defaults.env`

User-editable global Claude Code session env vars. Read at every Claude
session launch and merged into the session env. Lines beginning with `#`
are comments; `export ` prefix is accepted and stripped.

| | |
|---|---|
| **Path** | `<data_dir>/defaults.env` |
| **Permissions** | `0o600` |
| **Format** | Plain text, `KEY=VALUE` per line |
| **Reader** | `src/profiles/global_env.py:load_global_env` |
| **Writer** | `src/ui/claude/global_settings/_env_vars_tab.py` (on dialog accept); also deployed on first launch from the packaged resource `src/profiles/resources/defaults.env` |
| **Reset** | The "Reset to defaults" button in the Env Vars tab re-copies the packaged resource to disk |

Shipped defaults:

```
CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
CLAUDE_CODE_NO_FLICKER=1
```

### `ccproxy-requests.db`

SQLite database capturing every request/response that flows through the
embedded proxy. The dashboard and Recent Activity views read from this DB.

| | |
|---|---|
| **Path** | `<data_dir>/ccproxy-requests.db` |
| **Format** | SQLite 3 |
| **Reader** | `src/ccproxy/app/db/storage.py:Storage` |
| **Writer** | `src/ccproxy/app/middleware.py` (per request) |
| **Schema** | See `src/ccproxy/app/db/storage.py:_SCHEMA` for the live DDL; capture fields include request/response bodies, headers (with sensitive ones SHA-256 hashed — see [§ Sensitive header sanitization](#sensitive-header-sanitization)), timing, model name |

### `claude_profiles/<slug>/`

Per-profile Claude Code config directory. Each profile owns its own
subdirectory under `claude_profiles/`. The directory layout inside
`.claude/` matches Claude Code's standard `CLAUDE_CONFIG_DIR` structure.

#### `.claude/settings.json`

| | |
|---|---|
| **Path** | `<data_dir>/claude_profiles/<slug>/.claude/settings.json` |
| **Format** | JSON (Claude Code's standard schema) |
| **Reader** | Claude Code (at session start) |
| **Writer** | `src/profiles/config_dir.py:write_profile_config` (on profile save) |

Contains `model`, `availableModels`, `enabledPlugins`, and any free-form
keys from `profile.claude_settings`. `availableModels` lists ONLY human-
friendly aliases (not real upstream model IDs); aliases whose names don't
contain `claude` or `anthropic` get the synthetic suffix `-non-anthropic`
so they pass Claude Code's `/v1/models` discovery filter (see [§ Bug A:
synthetic IDs](#synthetic-ids-for-non-claude-aliases)).

#### `.claude/.mcp.json`

| | |
|---|---|
| **Path** | `<data_dir>/claude_profiles/<slug>/.claude/.mcp.json` |
| **Format** | JSON `{ "mcpServers": { "<name>": { ... } } }` |
| **Reader** | Claude Code |
| **Writer** | `src/profiles/config_dir.py:write_profile_config` (when the profile has MCP servers configured) |

Only written when the profile has at least one MCP server. The file is
deleted on the host when the profile's `mcp_servers` dict becomes empty
— Claude Code treats a missing file the same as an empty config.

#### `.claude/CLAUDE.md`

| | |
|---|---|
| **Path** | `<data_dir>/claude_profiles/<slug>/.claude/CLAUDE.md` |
| **Format** | Markdown |
| **Reader** | Claude Code |
| **Writer** | `src/profiles/docker_claude_md.py:sync_profile_claude_md` |

Composed at session-launch time as:

```
<contents of ~/.claude/CLAUDE.md (global)>
<!-- sharkdrown:profile-body-start -->
<profile.claude_md or profile body>
<!-- sharkdrown:profile-body-end -->
```

The global file lives at `~/.claude/CLAUDE.md` (host), edited via the
CLAUDE.md tab in Claude Settings.

#### `.claude/agents/<slug>.md`

| | |
|---|---|
| **Path** | `<data_dir>/claude_profiles/<slug>/.claude/agents/<slug>.md` |
| **Format** | Markdown with YAML frontmatter (`name`, `description`, `tools`, `model`) |
| **Reader** | Claude Code |
| **Writer** | `src/profiles/config_dir.py:write_profile_config` (one file per subagent) |

Stale agent files (those not in the current profile) are pruned on save.

#### `.claude/plugins/*.json`

Three independent files; missing fields on the profile are skipped so
empty plugin directories don't accumulate:

| File | Purpose |
|------|---------|
| `installed_plugins.json` | Tracks which marketplace plugins are installed and their versions |
| `known_marketplaces.json` | Registry of known marketplace sources the user can browse |
| `blocklist.json` | Blocklist of plugins that should not auto-install |

Writer: `src/profiles/config_dir.py:write_profile_config` (only when the
profile has the corresponding list non-empty).

#### `.claude/projects/...`

Claude Code's own per-session state. We do NOT write here — Claude Code
manages this directory itself. The proxy's conversation reader scans
`<data_dir>/claude_profiles/<slug>/.claude/projects/<cwd-hash>/` to read
back session transcripts for the conversations API.

### Container bind mounts

When Claude Code sessions run in a container (rather than the local PTY),
the profile directory is bind-mounted so Claude Code's `~/.claude` lands
at the expected in-container path:

| Host source | Container target | Notes |
|-------------|------------------|-------|
| `<data_dir>/claude_profiles/<slug>/` | `/home/claude/` | The `<slug>/.claude/` lands at `/home/claude/.claude/`. `--userns=keep-id` is passed to rootless podman so file ownership matches the host user. |
| `<data_dir>/` | `/etc/sharkdrown` | Mounted only when `run_proxy_in_container` is true; the proxy container reads `claude_global.json`, `profiles.json`, etc. |

The host-side Claude CLI is launched via `docker exec -i -t <container>
claude …` with the bind mount, `-t` flag (PTY allocation), and per-
session env vars (`-e` flags). `DockerSession.run` constructs the exec
command from the runtime config.

Writer: `src/ui/claude/docker_session.py:_get_bind_mounts`,
`src/ui/claude/docker_proxy.py:start_proxy_container`.

## User-level files

These live outside the app-data directory and follow the user's home:

### `~/.sharkdrown/logs/<label>_<epoch>.log`

Plain-text session log. Captures the raw byte stream from a Claude or
terminal session for post-mortem debugging.

| | |
|---|---|
| **Path** | `~/.sharkdrown/logs/<label>_<epoch>.log` |
| **Format** | Plain text, one chunk per write |
| **Reader** | User / debug tools (open in any editor) |
| **Writer** | `src/ui/terminal/session_logger.py:SessionLogger` |
| **Toggle** | The "Log Sessions" checkbox in the status bar (persisted via QSettings key `session_logging_enabled`, default `true`) |

### `~/.sharkdrown/recordings/session_<epoch>.cast`

asciicast v2 recording of the session. Replayable with `asciinema play`.

| | |
|---|---|
| **Path** | `~/.sharkdrown/recordings/session_<epoch>.cast` |
| **Format** | asciicast v2 (NDJSON) |
| **Reader** | `asciinema play` (external CLI) |
| **Writer** | `src/ui/terminal/session_recorder.py:SessionRecorder` (started via Terminal → Record Session) |

## QSettings (OS-native)

`QSettings("SharkDrown", "App")` is used for ephemeral UI state. On Linux
this is `~/.config/SharkDrown/SharkDrown.conf`; on macOS the standard
`~/Library/Preferences/com.SharkDrown.SharkDrown.plist`; on Windows the
registry under `HKEY_CURRENT_USER\Software\SharkDrown\SharkDrown`.

| Key | Default | Purpose |
|-----|---------|---------|
| `appearance/theme` | `"system"` | `ThemeMode` enum value |
| `app_font_base_size` | `12` | UI font base point size |
| `app_font_delta` | `0` | Delta from base size; ``setFont`` uses `base + delta` |
| `ui_font_family` | `"System Default"` | UI font family (see `Main → Settings → UI Font → Family`) |
| `content_font_family` | `"System Default"` | Content font family (see `Main → Settings → Content Font → Family`) |
| `session_logging_enabled` | `true` | Whether new sessions write a log file |
| `line_numbers_enabled` | `true` | Whether the editor gutter shows line numbers |
| `terminal/theme` | (varies) | Terminal color theme |
| `terminal/font_family` | `""` | Empty = system monospace |
| `terminal/font_size` | `0` | `0` = use defaults |
| `terminal/shell` | `""` | Default shell (empty = `$SHELL`) |
| `terminal/env_vars` | `""` | One `KEY=VALUE` per line |
| `clocks` | group | `ClockCollection`: master enable + list of clocks |

The QSettings store is per-user and per-app; SharkDrown never reads another
app's settings.

## Proxy configuration

The embedded FastAPI proxy (`src/ccproxy/`) reads its own config from
either `config.yaml`, `.env`, or environment variables. These files are
NOT in `<data_dir>` — they're discovered relative to the host process's
CWD or the configured `config_dir`.

### `config.yaml`

| | |
|---|---|
| **Path** | `<config_dir>/config.yaml` (typically CWD or app-data dir) |
| **Format** | YAML |
| **Reader** | `src/ccproxy/app/config.py:Settings` (pydantic-settings) |
| **Writer** | User-edited; SharkDrown does not auto-generate this file |

Discovery search order:

1. Explicit `config_dir` (from `GlobalSettings.config_dir`)
2. `<data_dir>`
3. Current working directory
4. Known parent directories containing `claude-code-proxy-py`

The first `config.yaml` found wins. See
[`find_config_yaml`](src/ccproxy/app/config.py) for the exact logic.

Precedence: init args → env vars → YAML → dotenv → field defaults.

### `.env`

Same discovery order as `config.yaml`. pydantic-settings parses it via
its built-in dotenv loader. Use this for secrets you don't want in YAML.

## Bug A: /tui mode defaulting to fullscreen

**Symptom:** "the /tui setting for Claude code sessions seems to be fullscreen instead of default"

**Root cause:** `defaults.env` ships `CLAUDE_CODE_NO_FLICKER=1`, which
forces Claude Code (since v2.1.110) into its fullscreen / alt-screen
flicker-free rendering path. The env var was added to fix flicker on
long sessions; the side-effect is that every Claude Code session runs
in `/tui fullscreen` mode without the user realising.

**Fix:** the `tui_mode` field on `claude_global.json` (see
[§ `claude_global.json`](#claude_globaljson)). Pick **Default** in
Claude Settings → General → Claude Code TUI mode to drop the env var
and use Claude Code's own non-fullscreen mode.

The override is applied in `src/profiles/env_builder.py:
build_claude_session_env` AFTER `defaults.env` is merged, so a user who
picks Default doesn't need to hand-edit `defaults.env`.

## Bug A: synthetic IDs for non-claude aliases

**Symptom:** non-claude model aliases (`minimax`, `freestuff`, `mm-m3`)
silently disappear from the `/model` picker even though `/v1/models`
returns them with `display_name`.

**Root cause:** Claude Code's discovery filter (relaxed in v2.1.223)
keeps entries whose `id` contains `claude` or `anthropic` (case-
insensitive). Aliases whose names don't contain either substring are
dropped from the cache file.

**Fix:** SharkDrown augments the alias map with synthetic companions —
`minimax` also becomes `minimax-non-anthropic` in `proxy_aliases`. Both
forms route to the same upstream model via the alias-rewrite layer. The
synthetic id passes the filter; the bare alias is what the user types
(`/model minimax`) and what `display_name` shows.

The synthetic-id scheme is the single source of truth at
[`src/profiles/discovery_id.py`](src/profiles/discovery_id.py) and is
applied in BOTH `settings.json.availableModels` (via
`config_dir.write_profile_config`) and `/v1/models` (via
`ui/main_window/_proxy_lifecycle.augment_proxy_aliases_for_discovery`).

## Sensitive header sanitization

Headers matching any of the substrings in `SENSITIVE_HEADER_TOKENS` (in
`src/ccproxy/app/utils/headers.py`) — including `authorization`,
`x-api-key`, `cookie`, `x-auth-token` — are SHA-256 hashed before being
written to `ccproxy-requests.db`. The captured value is the hex digest
prefixed with `sha256:`.

The replay endpoint (`/api/replay/{id}`) checks for this prefix before
forwarding; if the stored key was redacted, the replay returns 400
rather than sending the hash to the upstream as a silently-rejected
credential.

## Replay sentinel guard

See [§ Sensitive header sanitization](#sensitive-header-sanitization).

## Path traversal prevention (conversations API)

`ConversationReader` validates every user-supplied path segment
(`project`, `session_id`) with `Path.is_relative_to(self.base.resolve())`
before opening any file. Any segment that resolves outside the base
directory is rejected with a 400. Reader:
`src/ccproxy/app/conversations/reader.py`.

## Legacy migration

On first launch, if a `ProxyConfig` (QSettings-based) exists but no
`profiles.json`, the profile store performs automatic migration:

1. Reads legacy QSettings keys (host, port, model_endpoints, default_endpoint)
2. Creates a "Default" profile with migrated values
3. Writes `profiles.json`
4. Legacy QSettings remain untouched (non-destructive)

The migration is one-shot — subsequent edits go through the profile
editor and write to `profiles.json` only.

## Caveats

- The proxy must be started before Claude Code connects. The Qt app
  starts it automatically; for standalone use, start uvicorn first
  before launching Claude Code.
- `src/proxy/paths.py` uses `QStandardPaths` which requires
  `setOrganizationName` / `setApplicationName` to be called on the
  `QApplication` before `data_dir()` is invoked. `src/main.py` does
  this on startup.
- The Claude container runs as a non-root `claude` user; the bind-mount
  source must be owned by the matching host UID/GID or every file write
  surfaces as EACCES inside the container. SharkDrown's
  `docker_image_builder` rebuilds the image with
  `--build-arg CLAUDE_UID=$(id -u)` when the cached image's UID
  doesn't match.
- `defaults.env` is intentionally user-editable. Removing
  `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` will silence the
  `/v1/models` discovery cache; removing `CLAUDE_CODE_NO_FLICKER=1` is
  equivalent to setting `tui_mode=default` (see [§ Bug A](#bug-a-tui-mode-defaulting-to-fullscreen)).
