# Recommendations: Improvements, Bug Fixes & Opportunities

## Priority Legend

- **P0** — Correctness bug; could cause data loss or incorrect behavior
- **P1** — Important improvement; notable quality-of-life or security gain
- **P2** — Desirable; improves maintainability or developer experience
- **P3** — Nice-to-have; future consideration

---

## Bug Fixes & Correctness Issues

### 1. `sanitize_headers_dict` loses multi-value headers (P0)

**File:** `src/ccproxy/app/api/proxy.py:39-48`

The function only passes the first value of each header to `sanitize_headers()`, discarding any additional values. HTTP headers can legitimately have multiple values (e.g., `Set-Cookie`).

```python
# Current (buggy):
values = headers.getlist(key)
sanitized = sanitize_headers({key: values[0] if values else ""})
out[key] = [sanitized[key]]

# Fix: iterate all values
out[key] = [sanitize_headers({key: v})[key] for v in values]
```

### 2. Provider pattern matching is too greedy (P1)

**File:** `src/ccproxy/app/router/subagent.py:34-39`

`"o1"` and `"o3"` prefixes match any model starting with those characters. A hypothetical model like `"oracle-vision"` or `"o3de-render"` would incorrectly route to OpenAI.

**Fix:** Use exact-match-or-dash semantics:
```python
_PROVIDER_PATTERNS = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o1", "openai"),      # exact match
    ("o3-", "openai"),
    ("o3", "openai"),      # exact match
    ("claude-", "anthropic"),
)

def provider_for_model(model: str) -> str:
    for prefix, name in _PROVIDER_PATTERNS:
        if model == prefix or model.startswith(prefix + ("-" if not prefix.endswith("-") else "")):
            return name
    return "anthropic"
```

### 3. Stale subagent hash cache (P1)

**File:** `src/ccproxy/app/router/subagent.py`

`SubagentRouter` reads `~/.claude/agents/*.md` at construction time but never refreshes. If agents are edited mid-session, routing uses stale hashes. The proxy runs for the full desktop session lifetime (hours/days).

**Fix:** Add a file-watcher or periodic refresh (e.g., re-hash on each request if mtime changed, or every 60 seconds).

### 4. Port 0 race condition (P2)

**File:** `src/proxy/server_thread.py`, `src/ui/claude/claude_container_view.py`

When `port=0` (OS picks), the actual port isn't known until `started_ok(int)` fires. If the user triggers a Claude session launch between proxy start and the signal, `ANTHROPIC_BASE_URL` may be built with port 0.

The `flush_pending_launch` mechanism partially mitigates this, but a direct manual launch could race.

**Fix:** Disable the "New Claude Session" action until `started_ok` fires (grey out the menu item).

### 5. `chmod 600` is ineffective on Windows (P2)

**File:** `src/profiles/credential_store.py`, `src/profiles/store.py`

`os.chmod(path, 0o600)` is a no-op on Windows. The security claim of "owner-only access" doesn't hold.

**Fix:** On Windows, use `icacls` or the `win32security` module to set restrictive ACLs. Or document the limitation clearly.

---

## Code Quality Improvements

### 6. Extract ProfileEditorDialog tabs (P1)

**File:** `src/ui/claude/profile_editor_dialog.py` (3,613 lines)

This single file is the largest in the codebase. Each tab (General, Model Config, Providers, API Keys, Env Vars, Settings.JSON, Subagents) should be its own widget class in a `profile_editor/` package:

```
src/ui/claude/profile_editor/
├── __init__.py              # re-export ProfileEditorDialog
├── dialog.py                # Shell dialog with tab widget
├── general_tab.py           # ~200 lines
├── model_config_tab.py      # ~500 lines
├── providers_tab.py         # ~400 lines
├── api_keys_tab.py          # ~300 lines
├── env_vars_tab.py          # ~400 lines
├── settings_tab.py          # ~300 lines
└── subagents_tab.py         # ~400 lines
```

### 7. Extract MainWindow concerns (P2)

**File:** `src/ui/main_window.py` (1,421 lines)

Extract into:
- `ProxyLifecycleManager` — start/stop/restart logic
- `ProfileMenuManager` — profile switching, menu population
- `DocumentTabManager` — file open/save/new logic

### 8. Narrow exception handling (P2)

Throughout the codebase, `except Exception` with `# noqa: BLE001` is used broadly. Where the failure mode is known, use specific exceptions:

```python
# Instead of:
except Exception:  # noqa: BLE001
    profiles = []

# Use:
except (json.JSONDecodeError, KeyError, TypeError) as exc:
    logger.warning("corrupt profiles.json: %s", exc)
    profiles = []
```

### 9. Consolidate test infrastructure (P2)

17 test files each contain a custom `main()` runner. Consolidate to:
- Single `conftest.py` with fixtures for QApplication, temp dirs, free ports, httpx test client
- Remove all `if __name__ == "__main__"` runners
- Use pytest markers for slow tests (PTY, integration)

### 10. Add type stubs for pyte (P3)

**File:** `src/ui/terminal/renderer.py`

Create a `pyte.pyi` stub or use `pyright: ignore` pragmas so the renderer's Screen/Cell interface is type-checked.

---

## Architecture Improvements

### 11. Replace HTTP polling with EventBus bridge (P1)

**Files:** `src/proxy/activity_tile.py`, `src/ui/recent_activity_view.py`

The `ActivityTile` and `RecentActivityView` poll the proxy over HTTP every 3-5 seconds. Since the proxy is in-process, these could subscribe to the `EventBus` directly via a Qt signal bridge:

```python
class EventBusBridge(QObject):
    new_request = Signal(dict)

    def __init__(self, event_bus):
        super().__init__()
        event_bus.subscribe("new_request", self._on_event)

    def _on_event(self, data):
        self.new_request.emit(data)  # Thread-safe signal emission
```

Benefits: Zero-latency updates, no HTTP overhead, no timer threads.

### 12. Fix vendored import path (P2)

**File:** `src/proxy/server_thread.py:39-41`

The `sys.path` manipulation is fragile. Options:
- Make `ccproxy` a proper namespace package with `__init__.py` at `src/ccproxy/`
- Use relative imports: `from ..ccproxy.app.main import create_app`
- Add `src/` to the package's `[tool.setuptools.packages.find]` in pyproject.toml

### 13. Add FastAPI dependency injection (P2)

**File:** `src/ccproxy/app/api/proxy.py`

Route handlers currently access configuration via `request.app.state.*`. Using FastAPI's `Depends()` pattern makes dependencies explicit and testable:

```python
def get_router(request: Request) -> SubagentRouter:
    return request.app.state.router

@router.post("/v1/messages")
async def proxy_message(request: Request, router: SubagentRouter = Depends(get_router)):
    ...
```

### 14. Tie httpx client lifecycle to FastAPI lifespan (P2)

**Files:** `src/ccproxy/app/providers/anthropic.py`, `src/ccproxy/app/providers/openai.py`

Provider `httpx.AsyncClient` instances should be created in the FastAPI lifespan context manager and closed on shutdown, ensuring proper connection pool cleanup.

---

## Security Improvements

### 15. Tighten CORS origins (P1)

**File:** `src/ccproxy/app/main.py`

Even for a localhost tool, `allow_origins=["*"]` is vulnerable to DNS rebinding attacks. A malicious website could probe the proxy if the user visits it while the proxy runs.

```python
# Replace:
allow_origins=["*"]

# With:
allow_origins=["http://127.0.0.1", "http://localhost"]
allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$"
```

### 16. Optional keyring backend (P2)

**File:** `src/profiles/credential_store.py`

Add an opt-in `keyring` backend with graceful fallback:

```python
class CredentialStore:
    def __init__(self, path, use_keyring=False):
        if use_keyring:
            try:
                import keyring
                self._backend = KeyringBackend()
            except ImportError:
                self._backend = FileBackend(path)
        else:
            self._backend = FileBackend(path)
```

### 17. Add basic rate limiting (P3)

A simple token-bucket rate limiter (e.g., 100 req/s per client) would prevent accidental infinite loops from flooding the proxy and exhausting API quotas.

---

## Testing Improvements

### 18. Add CI/CD pipeline (P1)

No automated test execution exists. Add:

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - run: QT_QPA_PLATFORM=offscreen pytest --tb=short
      - run: ruff check src/
      - run: mypy src/ --ignore-missing-imports
```

### 19. OpenAI conversion integration test (P2)

**File:** `test_endpoint_routing.py` (extend)

Add an end-to-end test that sends an Anthropic-shaped request through the full proxy pipeline with an OpenAI-targeted endpoint, verifying the converted request matches expectations and the response transforms back correctly.

### 20. Fix test environment isolation (P2)

Some tests manipulate `os.environ` without cleanup. Use `monkeypatch` or `unittest.mock.patch.dict`:

```python
def test_env_builder(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # ... test logic ...
    # Automatic cleanup on test exit
```

---

## New Features & Opportunities

### 21. Token Cost Dashboard (P1)

The proxy captures token counts per request. Adding a price model enables:
- Per-session cost totals
- Per-profile cost tracking
- Daily/weekly cost trends
- Cost-per-model breakdown

Implementation: Add a `model_pricing` table or config, compute cost on each `update_request_with_response`, aggregate in `/api/stats`.

### 22. Request Replay (P2)

The SQLite capture has full request bodies. A "Replay" button on the dashboard could re-send a captured request to test routing changes without running a full Claude Code session.

### 23. Profile Import/Export (P2)

Allow exporting profiles (minus credentials) as shareable JSON/YAML for team standardization:

```python
def export_profile(profile: ClaudeProfile) -> dict:
    """Export profile without sensitive data."""
    data = asdict(profile)
    # Strip key_refs (credentials stay local)
    for p in data["providers"].values():
        p["key_ref"] = None
    return data
```

### 24. Provider Health Monitoring (P2)

Track per-provider metrics:
- Response latency (p50, p95, p99)
- Error rate (4xx, 5xx, timeouts)
- Availability status

Surface as a health indicator in the UI. Could enable automatic failover (route to backup provider when primary is degraded).

### 25. Conversation Search (P1)

The conversation viewer reads JSONL files but offers no search. Full-text search across conversation history would be high-value for finding past solutions and context.

Implementation: SQLite FTS5 index over conversation messages, or simple grep-style search with result highlighting.

### 26. Provider Plugin System (P3)

Currently adding a provider means editing source. A plugin interface would allow:
- Drop-in `providers/*.py` files discovered at startup
- Entry point registration via pyproject.toml extras
- Common interface: `class MyProvider(Provider)` with `forward()` method

### 27. Session Recording/Playback (P3)

Record terminal PTY output for session replay (like asciinema):
- Capture timestamped byte stream alongside rendered output
- Playback UI with speed controls
- Export as `.cast` files for sharing

### 28. Multi-Window Support (P2)

Allow detaching tabs into separate windows:
- Tear-off terminal tabs for multi-monitor workflows
- Detachable dashboard window
- Qt's `QDockWidget` or custom window management

### 29. Request Diff View (P3)

Compare two captured requests side-by-side:
- Highlight differences in model, parameters, system prompts
- Useful for debugging routing changes or model behavior differences
- JSON diff visualization with collapsible sections

### 30. Unified Dark/Light Theme (P2)

The terminal uses a dark theme, but Qt chrome uses system defaults. A unified theme system would:
- Provide consistent dark/light modes across all components
- Use Qt stylesheets or QPalette for the desktop shell
- Sync terminal palette with the selected theme
- Persist preference in profile or app settings
