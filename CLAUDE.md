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

## Caveats

- `QT_QPA_PLATFORM=offscreen` is set automatically in `tests/conftest.py`; tests don't need a display.
- The proxy must be started before Claude Code connects. The Qt app starts it automatically; for standalone use start uvicorn first.
- `src/proxy/paths.py` uses `QStandardPaths` which requires `setOrganizationName` / `setApplicationName` to be called on the `QApplication` before `data_dir()` is invoked.
- `ProviderCfg` uses `extra="forbid"` — YAML typos in provider blocks raise a validation error at startup rather than silently being ignored.
