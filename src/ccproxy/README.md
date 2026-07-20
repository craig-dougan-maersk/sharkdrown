# Vendored claude-code-proxy-py

This directory contains a vendored copy of
[claude-code-proxy-py](https://github.com/...) — a transparent FastAPI
proxy for Claude Code with SQLite request capture and an htmx dashboard.

It is embedded directly into the sharkdrownv3 desktop app so users no longer
need to run the proxy via Docker or `uvicorn`. The desktop launches it
inside a `QThread` (`src/proxy/server_thread.py`) and embeds its dashboard
inside a `QDockWidget`.

## Vendoring policy

- **Source of truth**: `/home/craig/stuff/tooling/claude-code-proxy-py/`
- **Sync procedure** (manual):
  ```bash
  rsync -a --delete \
    /home/craig/stuff/tooling/claude-code-proxy-py/app/ \
    src/ccproxy/app/
  # Rewrite intra-package imports
  find src/ccproxy/app -name '*.py' -exec sed -i \
    -e 's/^from app\./from ccproxy.app./g' \
    -e 's/^import app\./import ccproxy.app./g' \
    -e 's/^[[:space:]]*from app\./from ccproxy.app./g' \
    -e 's/^[[:space:]]*import app\./import ccproxy.app./g' {} +
  # Defer eager app construction
  sed -i 's/^app = create_app()$/app = None  # deferred/' \
    src/ccproxy/app/main.py
  ```
- **Why we defer `create_app()`**: the upstream `app/main.py` ends with
  `app = create_app()` which runs `Settings()` (file I/O) and constructs
  two `httpx.AsyncClient` instances at import time. Calling it from
  `src/proxy/server_thread.py` lets us control the SQLite path and
  defer all of this work until the user clicks **Server → Start**.

## Layout

```
src/ccproxy/
├── README.md        ← this file
└── app/             ← the vendored proxy package
    ├── main.py
    ├── config.py
    ├── middleware.py
    ├── api/         ← route handlers
    ├── db/          ← aiosqlite storage
    ├── providers/   ← Anthropic + OpenAI
    ├── router/      ← subagent routing
    ├── conversations/  ← ~/.claude JSONL reader
    ├── streaming/   ← SSE capture
    ├── live/        ← event bus
    ├── utils/
    ├── models/      ← Pydantic models
    ├── templates/   ← Jinja2 (dashboard HTML)
    └── static/      ← css + js
```

## How the desktop app uses this

```python
from ccproxy.app.main import create_app  # noqa: E402

app = create_app(storage_path=Path("/path/to/ccproxy.db"))
config = uvicorn.Config(app=app, host="127.0.0.1", port=3001, lifespan="on")
server = uvicorn.Server(config)
await server.serve()
```

`src/proxy/server_thread.py` does this inside a `QThread`-owned asyncio loop.

## Dashboard assets

The dashboard templates load Tailwind, htmx, and Alpine.js from public CDNs.
This requires internet access on first paint. If offline operation becomes
a requirement, vendor these assets under `app/static/vendor/` and rewrite
`app/templates/base.html`.

## Known concerns from upstream

- `app/middleware.py` uses `BaseHTTPMiddleware`, which Starlette 1.3.x
  deprecated. The proxy still works; future sync may need a rewrite to
  `ASGIApp`/`Message`-based middleware.
- The proxy assumes Python 3.12+. sharkdrownv3's `pyproject.toml` pins
  `requires-python = ">=3.12"` to match.