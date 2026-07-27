# Component Reference

## Proxy Engine (`src/ccproxy/`)

The embedded proxy is a vendored FastAPI application that intercepts Claude Code API traffic.

### Provider Abstraction (`src/ccproxy/app/providers/`)

| Class | File | Purpose |
|-------|------|---------|
| `Provider` (ABC) | `base.py` | Abstract interface: `forward(body, headers, stream, target_url, target_api_key)` |
| `AnthropicProvider` | `anthropic.py` | Pass-through forwarding with hop-by-hop header stripping, `anthropic-version` injection, redirect retry (MiniMax pattern) |
| `OpenAIProvider` | `openai.py` | Full Anthropic↔OpenAI protocol conversion (request, response, streaming) |

All user-defined providers (MiniMax, OpenRouter, etc.) are instantiated as `AnthropicProvider` since they use the Anthropic wire format.

### Routing Pipeline (`src/ccproxy/app/router/`)

- **`SubagentRouter`** — Inspects each request to determine the target provider. Decision priority: subagent detection → endpoint mapping → default provider → prefix patterns.
- **`RouteDecision`** — Pydantic model carrying the routing result (provider instance, original/target model, path type, provider name).

### Storage (`src/ccproxy/app/db/`)

- **`StorageService`** — Async CRUD over SQLite (aiosqlite, WAL mode). Single `requests` table (schema v2, 22 columns). Methods: `save_request`, `get_requests` (paginated+filtered), `update_request_with_response`, `count_total/failed/no_tokens`.
- **Schema migrations** — v1→v2 added `input_tokens`, `output_tokens`, `total_tokens` columns.

### Streaming Capture (`src/ccproxy/app/streaming/`)

- **`stream_and_capture()`** — Yields SSE bytes to the client while accumulating `data:` lines for storage. Handles chunk boundaries that split SSE records mid-line.

### Dashboard (`src/ccproxy/app/templates/`, `src/ccproxy/app/api/dashboard.py`)

Server-rendered HTML with:
- **Jinja2** templates for page structure
- **htmx** for partial page loads (request list, detail modals, conversations)
- **Alpine.js** for reactive client-side state (filters, toggles)
- **EventSource** (SSE) for live request feed

### Live Events (`src/ccproxy/app/live/`)

- **`EventBus`** — In-process pub/sub. Proxy handler publishes `new_request` events; SSE endpoint subscribes and forwards to connected browsers.

### Middleware (`src/ccproxy/app/middleware.py`)

- **`BodyCaptureMiddleware`** — Pre-reads POST/PUT/PATCH bodies into `request.state.body_bytes` so route handlers don't need to consume the stream.
- **`AccessLogMiddleware`** — One ANSI-colored log line per request with method, path, status, and duration.

### Utilities (`src/ccproxy/app/utils/`)

- **`sanitize_headers()`** — SHA-256 hashes sensitive header values (API keys, auth tokens) before storage.
- **`generate_request_id()`** — 16-char hex via `secrets.token_hex(8)`.

---

## Profile System (`src/profiles/`)

Manages multiple Claude Code session configurations with distinct providers, credentials, and settings.

### Data Model (`models.py`)

| Dataclass | Purpose |
|-----------|---------|
| `Provider` (frozen) | Named upstream service — URL, key_ref, available_models catalog |
| `ModelEndpoint` (frozen) | Model routed through a Provider + optional aliases |
| `Subagent` | Agent definition (name, description, system_prompt, tools, model) |
| `ClaudeProfile` | Complete profile: providers, endpoints, env_vars, settings, subagents, bind config |

Utility: `slugify(name)` — filesystem-safe identifier derived from profile name.

### Profile Store (`store.py`)

- **`ProfileStore`** — CRUD facade over `profiles.json` (chmod 600). Supports: load, save, set_active, upsert, delete. Always ensures a "Default" profile at index 0. Performs first-launch migration from legacy `ProxyConfig`.
- Atomic writes: `.tmp` → `os.replace()` → `chmod 600`.

### Credential Store (`credential_store.py`)

- **`CredentialStore`** — Plaintext JSON file (chmod 600) for API keys. Keys referenced by stable `ref` strings (not friendly names). Public API: `get(ref)`, `set(ref, value)`, `delete(ref)`, `all_refs()`.
- Keys are whitespace-stripped and split-rejoined to handle copy-paste issues.

### Environment Builder (`env_builder.py`)

- **`resolve_api_key(profile, credential_store)`** — Walks endpoint → provider → key_ref chain.
- **`build_claude_session_env(profile, ...)`** — Assembles complete env dict with layered precedence: `os.environ` → infra vars → `profile.env_vars` → session overrides. Safety net re-applies infra vars on top.
- **Infrastructure vars**: `ANTHROPIC_BASE_URL`, `CLAUDE_CONFIG_DIR`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_AUTH_TOKEN`, `TERM`, `COLORTERM`.

### Config Dir Generator (`config_dir.py`)

- **`write_profile_config(profile, base_dir)`** — Renders `<slug>/settings.json` with `availableModels` (aliases only) and `model`. Renders `agents/<name>.md` with YAML frontmatter. Prunes stale agent files.

### Environment Variable Catalog (`env_var_catalog.py`)

- **`StandardEnvVar`** (frozen dataclass) — Curated catalog of ~60 Claude Code env vars organized by section. Powers the "Add Standard..." picker in the profile editor.

---

## Terminal Emulator (`src/ui/terminal/`)

Full VT100/xterm terminal emulation embedded in the Qt UI.

### PTY Backend (`pty_backend.py`)

- **`PtySession(QThread)`** — POSIX PTY via `pty.fork()`. Uses `select()` loop with self-pipe for clean shutdown. Signals: `data_ready(bytes)`, `finished_signal(int)`. Supports `write(bytes)` and `resize(rows, cols)` via `TIOCSWINSZ`.
- Shutdown: SIGHUP → wait → SIGKILL escalation.

### Renderer (`renderer.py`)

- **`screen_to_html(screen, cursor, show_cursor, selection)`** — Pure-Python, Qt-free function. Converts pyte `Screen` to HTML `<pre>` with `<span>` runs grouped by identical styling.
- Features: 16-color + 256-color palette, dark theme CSS variables, block/linear selection highlighting, cursor rendering with reversed colors.
- Deliberately suppresses SGR-4 underline (Claude Code v2.x leaves it on globally).

### Session View (`terminal_session_view.py`)

- **`TerminalSessionView(QWidget)`** — Owns `PtySession` + `pyte.Screen` + `pyte.ByteStream`. 60fps repaint via QTimer. Handles resize debouncing.
- **`TerminalTextEdit(QTextEdit)`** — Custom widget that intercepts keypresses and translates them to PTY-compatible byte sequences (Ctrl+C → 0x03, arrows → CSI, etc.).

### Container (`terminal_container_view.py`)

- **`TerminalContainerView(QWidget)`** — Toolbar + closable tab strip. Manages multiple terminal sessions with tab cycling (Alt+Left/Right).

---

## Editor (`src/ui/widgets/`, `src/core/`)

### Document Model (`src/core/document.py`)

- **`DocumentTab`** — Represents one open file/tab. Tracks content, file path, modification state, and content type (markdown/JSON/YAML).

### Editor Widget (`src/ui/widgets/editor_widget.py`)

- **`EditorWidget(QWidget)`** — Text editor with syntax highlighting. Supports `MarkdownHighlighter`, `JSONHighlighter`, `YAMLHighlighter` (Pygments-based). Emits `content_changed` signal for live preview.

### Preview Widget (`src/ui/widgets/preview_widget.py`)

- **`PreviewWidget(QWidget)`** — Renders markdown to HTML with pandas table execution. Fenced code blocks marked `python pandas` are executed and their DataFrame output rendered as HTML tables.

---

## UI Shell (`src/ui/main_window.py`)

### MainWindow (1,421 lines)

Central orchestrator managing:

- **Three top-level tabs**: Documents, Terminals, Claude
- **Proxy lifecycle**: Start/stop/restart via `ProxyServerThread`
- **Profile store**: Load/save/switch profiles, credential resolution
- **Menu system**: File, Edit, View, Terminal, Claude, Proxy menus
- **Activity monitoring**: `ActivityTile` in status area

### Claude Container (`src/ui/claude/claude_container_view.py`)

- **`ClaudeContainerView(QWidget)`** — Hosts pinned tabs (Dashboard, Recent Activity) plus closable Claude Code session tabs.
- **Pending launch queue**: If a session is requested before the proxy is ready, it's queued and launched once `started_ok` fires.
- **Binary guard**: Validates `claude` is on PATH before launching.

### Profile Editor (`src/ui/claude/profile_editor_dialog.py`)

- **`ProfileEditorDialog(QDialog)`** (3,613 lines) — Tabbed editor with sections: General, Model Config, Providers, API Keys, Env Vars, Settings.JSON, Subagents. Validates alias uniqueness on save.

---

## Proxy Lifecycle Widgets (`src/proxy/`)

### Server Thread (`server_thread.py`)

- **`ProxyServerThread(QThread)`** — Runs uvicorn with its own asyncio loop. Builds `Settings` from discovered config files + per-profile overrides. Signals: `started_ok(int)`, `failed(str)`, `stopped()`.

### Activity Tile (`activity_tile.py`)

- **`ActivityTile(QWidget)`** — Status bar widget polling `/api/stats` every 3s. Displays request count with color coding (green=healthy, yellow=some failures, red=all failing).

### Dashboard View (`dashboard_view.py`)

- Embeds the proxy's HTML dashboard in a `QWebEngineView`.

### Config Discovery (`paths.py`)

- **`find_config_yaml(explicit_dir)`** / **`find_env_file(explicit_dir)`** — Multi-location search for proxy configuration (data dir → CWD → known parent directories).

### Legacy Config (`proxy_config.py`)

- **`ProxyConfig`** — QSettings-based configuration from the pre-profile era. Still used for first-launch migration.
