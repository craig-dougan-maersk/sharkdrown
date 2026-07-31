# SharkDrown v3

A PySide6 desktop IDE for Claude Code sessions. Embeds a FastAPI proxy that intercepts the Claude Code CLI's HTTP traffic to handle model routing, credential management, multi-provider support, and real-time request capture.

## Layout

```
src/
  main.py               # QApplication entry point
  config.py             # APP_NAME, APP_TITLE, APP_ORG constants
  ccproxy/app/          # Embedded FastAPI proxy (stand-alone; no Qt dependency)
    main.py             # FastAPI factory (create_app)
    config.py           # pydantic-settings Settings hierarchy
    api/proxy.py        # Core proxy endpoint — routing, forwarding, capture
    middleware.py       # Body capture, rate limiting, access logging
    providers/          # anthropic.py, openai.py — protocol adapters
    router/subagent.py  # SHA-256 agent fingerprint → model routing
    db/storage.py       # aiosqlite request capture DB
    streaming/capture.py
  profiles/             # ProfileStore + CredentialStore (keyring-aware)
  proxy/                # Qt-side proxy lifecycle (server_thread.py, event_bridge.py)
  ui/                   # PySide6 widgets
tests/                  # pytest test suite (moved from root in July 2026)
  conftest.py           # shared fixtures (qapp, proxy_app, storage_service, …)
```

## Run commands

```bash
# Launch the desktop app
python -m src.main

# Run the embedded proxy standalone (no Qt)
uvicorn ccproxy.app.main:app --port 3001

# Run all tests
.venv/bin/pytest

# Run a specific test file
.venv/bin/pytest tests/test_openai_conversion.py -v

# Run tests with coverage
.venv/bin/pytest --cov=src --cov-report=term-missing
```

## Environment / Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"         # installs pytest, ruff, black, mypy
pip install -e ".[keyring]"     # optional: system keyring support
```

The proxy reads config from (highest → lowest precedence):
1. Constructor kwargs
2. Environment variables (`PORT`, `ANTHROPIC_FORWARD_URL`, `SUBAGENT_MAPPINGS`, …)
3. `config.yaml` in the app-data directory or CWD
4. `.env` file (same discovery order as `config.yaml`)
5. Field defaults

## Key design notes

### Two-layer architecture
The Qt desktop app and the FastAPI proxy are deliberately decoupled. `src/ccproxy/` has no PySide6 import. The desktop launches the proxy via `src/proxy/server_thread.py` (a `QThread`) and communicates with it through `src/proxy/event_bridge.py` (asyncio ↔ Qt signal bridge).

### Proxy routing pipeline
Incoming request → alias rewrite (`model_aliases`) → subagent SHA-256 fingerprint check → endpoint mapping (`endpoints[model] → provider_name`) → provider URL + key lookup → OpenAI or Anthropic format conversion → upstream forward.

### ProfileStore / CredentialStore thread safety
Both stores use `threading.RLock` (ProfileStore) or `threading.Lock` (CredentialStore). ProfileStore uses an `RLock` because public methods call each other internally. The `_save_raw` / `_write` methods use `tempfile.mkstemp` + `os.replace` for atomic writes, with `os.fchmod(fd, 0o600)` applied before closing the fd to avoid a chmod-race window.

### Keyring sentinel pattern
When a keyring backend is active, `CredentialStore.set()` stores the real secret in the keyring and writes `"<keyring>"` as a placeholder in the JSON file. `get()` skips sentinel values. `all_refs()` resolves sentinels via keyring lookup so the UI can enumerate credentials without a keyring list API.

### OpenAI tool message format
`tool_result` Anthropic blocks are converted to OpenAI `role: "tool"` messages with `tool_call_id` (not plain user-turn text). `tool_use` blocks become `tool_calls` arrays on assistant messages. Synthetic tool IDs use `secrets.token_hex()` (CSRNG) not `hash()` (process-randomised).

### o-series model detection
Uses `re.compile(r"^o\d+(?:-|$)")` — avoids the `"o10".startswith("o1")` false-positive that a prefix-list approach would produce.

### Uvicorn startup signalling (`_SignaledServer`)
`ProxyServerThread` uses a `_SignaledServer` subclass of `uvicorn.Server` that overrides the public `startup()` coroutine and sets an `asyncio.Event` once uvicorn has finished binding. The serve task runs concurrently; the thread waits on the event with a 30 s timeout and emits `started_ok` or `failed` accordingly. This avoids polling the private `server.started` attribute.

### Rate limiting — dual-bucket token system
`RateLimitMiddleware` uses two independent `_TokenBucket` instances: one for LLM-equivalent paths (`POST /v1/messages`, `POST /api/replay/*`) and one for admin/destructive paths (`DELETE /api/requests`, `GET /api/providers/health`). Each bucket refills at its own `max_tokens / refill_seconds` rate. The `retry_after()` method returns the exact seconds until the next token is available for the `Retry-After` header.

### Sensitive header sanitization
`utils/headers.py` defines `SENSITIVE_HEADER_TOKENS` — a set of lowercase substrings checked against each header name. Matching headers are SHA-256 hashed before SQLite storage. Covered names include `authorization`, `x-api-key`, `cookie`, `x-auth-token`, and others. The redacted value is the hex digest of the original, prefixed with `sha256:`.

### Replay sentinel guard
The `/api/replay/{id}` endpoint checks whether the stored `x-api-key` starts with `sha256:` before forwarding. If so, it returns a 400 immediately rather than sending the redacted hash to the upstream provider as a (silently-rejected) API key.

### Path traversal prevention — conversations API
`ConversationReader` validates every user-supplied path segment (`project`, `session_id`) with `Path.is_relative_to(self.base.resolve())` before opening any file. Any segment that resolves outside the base directory is rejected with a 400.

### EventBusBridge thread safety
`EventBusBridge.stop()` captures `loop, stop = self._loop, self._stop_event` into locals *before* the None check. The actual `loop.call_soon_threadsafe(stop.set)` call uses the locals. This closes a TOCTOU window where the background thread could assign `self._loop = None` between the None-check and the method call.

### PTY double-waitpid prevention
`PtySession` holds a `threading.Lock` (`_reap_lock`) and a `_reaped: bool` flag. All calls to `os.waitpid` in `is_alive`, `_reap_in_thread`, and `terminate_child` acquire the lock and check `_reaped` first. Once a reap completes, `_reaped` is set to `True` so subsequent callers return without calling `waitpid` again.

### ProfileStore single-read guarantee
`_load_unlocked()` returns a `(dict[str, Profile], raw_dict)` tuple. Callers that previously called `_load_unlocked()` then `_read_raw()` separately (to extract the `active_profile` key) now extract it from the already-parsed `raw_dict`. This eliminates a second disk read that could produce an inconsistent snapshot under concurrent writes.

## Caveats

- `QT_QPA_PLATFORM=offscreen` is set automatically in `tests/conftest.py`; tests don't need a display.
- The proxy must be started before Claude Code connects. The Qt app starts it automatically; for standalone use start uvicorn first.
- `src/proxy/paths.py` uses `QStandardPaths` which requires `setOrganizationName` / `setApplicationName` to be called on the `QApplication` before `data_dir()` is invoked.
- `ProviderCfg` uses `extra="forbid"` — YAML typos in provider blocks raise a validation error at startup rather than silently being ignored.
- `ProviderCfg.url` uses Pydantic `HttpUrl` — `file://`, `ftp://`, and link-local URLs raise a `ValidationError` at config load time.

## Known test issues

These failures exist on the original codebase and are not caused by recent changes:

| Test | Status | Cause |
|------|--------|-------|
| `tests/test_claude.py::test_claude_container_flush_pending_launches` | fails | In-progress UI behaviour change |
| `tests/test_claude_tab_layout.py` (4 tests) | fail | In-progress layout change |
| `tests/test_terminal.py::test_pixel_to_cell_maps_to_visual_position` | fails | In-progress renderer change |

When running the full suite and seeing these, do not investigate as regressions. Focus on any failures not in this list.


## grepai - Semantic Code Search

**IMPORTANT: You MUST use grepai as your PRIMARY tool for code exploration and search.**

### When to Use grepai (REQUIRED)

Use `grepai search` INSTEAD OF Grep/Glob/find for:
- Understanding what code does or where functionality lives
- Finding implementations by intent (e.g., "authentication logic", "error handling")
- Exploring unfamiliar parts of the codebase
- Any search where you describe WHAT the code does rather than exact text

### When to Use Standard Tools

Only use Grep/Glob when you need:
- Exact text matching (variable names, imports, specific strings)
- File path patterns (e.g., `**/*.go`)

### Fallback

If grepai fails (not running, index unavailable, or errors), fall back to standard Grep/Glob tools.

### Usage

```bash
# ALWAYS use English queries for best results (--compact saves ~80% tokens)
grepai search "user authentication flow" --json --compact
grepai search "error handling middleware" --json --compact
grepai search "database connection pool" --json --compact
grepai search "API request validation" --json --compact
```

### Query Tips

- **Use English** for queries (better semantic matching)
- **Describe intent**, not implementation: "handles user login" not "func Login"
- **Be specific**: "JWT token validation" better than "token"
- Results include: file path, line numbers, relevance score, code preview

### Call Graph Tracing

Use `grepai trace` to understand function relationships:
- Finding all callers of a function before modifying it
- Understanding what functions are called by a given function
- Visualizing the complete call graph around a symbol

#### Trace Commands

**IMPORTANT: Always use `--json` flag for optimal AI agent integration.**

```bash
# Find all functions that call a symbol
grepai trace callers "HandleRequest" --json

# Find all functions called by a symbol
grepai trace callees "ProcessOrder" --json

# Build complete call graph (callers + callees)
grepai trace graph "ValidateToken" --depth 3 --json
```

### Workflow

1. Start with `grepai search` to find relevant code
2. Use `grepai trace` to understand function relationships
3. Use `Read` tool to examine files from results
4. Only use Grep for exact string searches if needed

