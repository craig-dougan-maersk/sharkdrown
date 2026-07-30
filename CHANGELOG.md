# Changelog

All notable changes to SharkDrown are documented in this file.

## [Unreleased]

### Security

- **Header sanitisation extended** — `cookie` and `x-auth-token` are now included in `SENSITIVE_HEADER_TOKENS` and SHA-256 replaced before storage (`utils/headers.py`)
- **SSRF prevention via `HttpUrl`** — `ProviderCfg.url` now uses Pydantic `HttpUrl`, rejecting `file://`, `ftp://`, and link-local targets at config load time rather than after the first request (`ccproxy/app/config.py`)
- **Replay sentinel guard** — `/api/replay/{id}` returns a 400 with a clear error when the stored API key is the SHA-256 redaction sentinel, preventing silent 401s from replaying sanitised keys (`api/dashboard.py`)
- **Rate limiting extended to LLM-equivalent paths** — `POST /api/replay/{id}`, `DELETE /api/requests`, and `GET /api/providers/health` are now rate-limited via a dual-bucket token system (separate LLM vs admin buckets) in `RateLimitMiddleware` (`middleware.py`)
- **API key chars removed from logs** — Partial key logging (`key[:4]`, `key[-4:]`) removed from the OpenAI provider request path (`providers/openai.py`)
- **Path traversal prevention in conversations API** — `project` and `session_id` path segments are validated against the base directory with `Path.is_relative_to()` before any file is opened (`api/dashboard.py`, `conversations/reader.py`)

### Fixed

#### Proxy reliability
- **EventBusBridge stop() race** — `self._loop` and `self._stop_event` are captured into locals before the None check in `stop()`, eliminating a TOCTOU window where a background thread could zero the attribute between check and call (`proxy/event_bridge.py`)
- **Uvicorn startup detection** — Replaced polling `getattr(server, "started", False)` (private API) with a `_SignaledServer` subclass that overrides the public `startup()` coroutine and sets an `asyncio.Event`; startup now times out cleanly after 30 s (`proxy/server_thread.py`)
- **Configured timeouts now reach HTTP clients** — `settings.server.timeouts.read` is passed as `timeout_seconds` to all `AnthropicProvider` and `OpenAIProvider` constructors; previously the setting was read but silently ignored (`ccproxy/app/main.py`)
- **Unguarded `request.state.body_bytes`** — Access now uses `getattr(request.state, "body_bytes", b"")` so middleware re-ordering or early exceptions no longer raise `AttributeError` in the proxy handler (`api/proxy.py`)

#### PTY / terminal
- **Double-`waitpid` race in `PtySession`** — `is_alive`, `_reap_in_thread`, and `terminate_child` now share a `threading.Lock` with a `_reaped` flag so only one caller ever calls `os.waitpid`, preventing spurious `ChildProcessError` (`ui/terminal/pty_backend.py`)

#### Profile / credential store
- **Double disk read in `ProfileStore`** — `_load_unlocked()` now returns a `(profiles, raw_data)` tuple so the active profile name is extracted from the already-parsed dict, eliminating a second `_read_raw()` call that could produce an inconsistent snapshot under concurrent writes (`profiles/store.py`)
- **Credential whitespace corruption** — Normalisation changed from `"".join(value.split())` (strips interior spaces) to `value.strip()` (trims only leading/trailing), preventing corruption of OAuth tokens and multi-word credentials (`profiles/credential_store.py`)

#### UI state
- **Start button incorrectly re-enabled while proxy is running** — `_on_proxy_started()` now calls `action_start_proxy.setEnabled(False)` so the Start action is disabled while the proxy is up, preventing misleading dual-enabled button state (`ui/main_window/_proxy_lifecycle.py`)

#### Code hygiene
- **Dead `get_storage_service()` removed** — The function opened a hardcoded `requests.db` path and was never called by live code; removed to prevent accidental second SQLite writer (`db/storage.py`)
- **Duplicate `router` assignment removed** — First `router = APIRouter(...)` line in `api/monitor.py` was immediately overwritten; removed (`api/monitor.py`)

### Test suite

- **`test_end_to_end.py` now pytest-discoverable** — E2E scenario functions renamed to `test_*` pattern; `test_proxy_start_stop_cycle` wrapper added
- **`QMessageBox.warning` patch scoped to fixture** — Module-level monkey-patch in `test_profile_model_and_dialog.py` moved to a `monkeypatch` fixture to prevent poisoning other tests in the same session
- **`pytest.skip()` replaces bare `return`** — `test_config_discovery.py` now properly marks the test skipped when `config.yaml` is absent instead of silently passing with zero assertions
- **`export_profile` / `import_profile` tests added** — `tests/test_profile_export_and_keyring.py` covers round-trip backup/restore, credential-stripping, and version header acceptance/rejection
- **Keyring backend tests added** — `_KeyringBackend`, `_keyring_available()`, and sentinel resolution now tested via `unittest.mock.patch` on `keyring.get_credential` / `keyring.set_password`
- **`EventBusBridge` lifecycle tests added** — `tests/test_event_bridge_lifecycle.py` covers start-then-stop, double-stop, stop-before-start, and stop-after-thread-exit paths
- **`tempfile.mkdtemp()` leaks replaced** — Storage fixture helpers now use pytest `tmp_path` for automatic cleanup
- **Hardcoded `/tmp/` QSettings paths replaced** — `test_app.py` and `test_endpoint_routing.py` now pass `tmp_path / "settings"` so test runs do not share state
- **Duplicate `_find_free_port()` removed** — `test_proxy_integration.py` now uses the shared `free_port` conftest fixture
- **`pytest-asyncio>=0.23` added to dev deps** — Async test support now available without `asyncio.run()` wrappers

### Configuration

- **Package name corrected** — `pyproject.toml` `name` was `pandas-markdown-editor` (stale template value); corrected to `sharkdrown`
- **`requirements.txt` eliminated as duplicate source of truth** — File now redirects to `pip install -e ".[dev]"`; `pyproject.toml` is the single install spec
- **mypy tightened** — Added `disallow_untyped_defs`, `check_untyped_defs`, `ignore_missing_imports`, `warn_return_any`
- **ruff rule set made explicit** — `select = ["E", "W", "F", "I", "B", "UP"]` added to `[tool.ruff.lint]`
- **Coverage config added** — `[tool.coverage.run]` section added; `--cov=src` included in default `addopts`

### Added

#### Document Editor
- New File dialog (Ctrl+N) with file type picker: Markdown, JSON, YAML, Plain Text
- Coloured tab icons indicating file type (blue=MD, orange=JSON, green=YAML, grey=text)
- Double-click tab to rename
- Unsaved changes guard — prompts Save/Discard/Cancel on close
- Markdown formatting toolbar (H1–H3, Bold, Italic, Lists, Link, Table)
- Built-in linting panel (Ctrl+Shift+L) for JSON, YAML, and Markdown
- Click-to-navigate from lint errors to source line
- Status bar: word count, line count, cursor position (Ln/Col)
- Per-tab cursor position memory (preserved across tab switches)
- Plain Text file format support (.txt)
- Starter content templates for new JSON (`{}`) and YAML (`---`) files

#### Terminal Emulator
- Terminal Settings dialog (theme, font, default shell, environment variables)
- Five built-in terminal themes (Dark, Solarized Dark/Light, Monokai, Nord)
- Auto-close terminal tab when shell exits (typing `exit` or Ctrl+D)
- Linear text selection by default (block selection with Alt/Cmd modifier)
- Scrollback history support (5000 lines via pyte HistoryScreen)
- Plain-text session logging to ~/.sharkdrown/logs/
- oh-my-posh / prompt framework compatibility (TERM=xterm-256color)
- Horizontal scroll fix — characters no longer cut off on resize

#### Claude Sessions & Proxy
- Multi-provider proxy routing (Anthropic, OpenAI-compatible, custom)
- Profile system for credentials and routing rules
- Model aliasing and subagent-aware routing
- Anthropic ↔ OpenAI protocol conversion
- Streaming request/response capture to SQLite
- Token usage tracking (input/output/total)
- Activity tile in status bar
- Auto-start proxy option
- Dashboard API for request history

#### Application
- Three-workspace layout: Documents, Terminals, Claude
- Application theme system (Dark, Light, System)
- UI font family and size controls (separate from content font)
- Content font size controls (Ctrl+=/-, status bar slider)
- Session logging toggle in status bar
- Keyboard shortcut for new terminal (Ctrl+Alt+T)
- Alt+Left/Right terminal session cycling

### Changed
- File → New now shows type picker dialog instead of creating blank markdown
- Default format for unknown extensions changed from "markdown" to "text"
- Tab title shows modification indicator (● prefix) for unsaved changes

### Fixed
- Terminal text cutoff on resize (horizontal scroll caused by ensureCursorVisible)
- oh-my-posh statusline disappearing after commands (stale scrollbar.maximum)
- Text selection modifier detection reliability (using _norm_mods for consistent bitwise ops)
