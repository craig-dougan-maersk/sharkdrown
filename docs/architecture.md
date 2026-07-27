# SharkDrown v3 — System Architecture

## Overview

SharkDrown is a PySide6 (Qt6) desktop application that functions as a **Claude Code management IDE**. Its primary capability is an embedded man-in-the-middle proxy that intercepts all Claude Code API traffic, enabling multi-provider routing, model aliasing, subagent-aware routing, and full request/response capture.

Despite its `pyproject.toml` name (`pandas-markdown-editor`), the markdown editor is a secondary feature. The application's core value is managing Claude Code sessions with instrumented proxy routing.

## Two-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 1: PySide6 Desktop Application                               │
│                                                                     │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │  Editor  │  │   Terminal   │  │    Claude    │  │  Profile  │  │
│  │  Tabs    │  │   Emulator   │  │   Sessions   │  │  Manager  │  │
│  └──────────┘  └──────┬───────┘  └──────┬───────┘  └─────┬─────┘  │
│                        │                 │                 │        │
│                    PTY fork          PTY fork       JSON/credentials│
│                        │                 │                 │        │
│  ┌─────────────────────┴─────────────────┴─────────────────┘        │
│  │  MainWindow (orchestrator)                                       │
│  │  - Proxy lifecycle (start/stop/restart)                          │
│  │  - Profile store integration                                     │
│  │  - Menu/tab management                                           │
│  └──────────────────────────┬───────────────────────────────────────┘
│                             │
│              QThread + asyncio.run()
│                             │
├─────────────────────────────┼───────────────────────────────────────┤
│                             │                                       │
│  Layer 2: Embedded FastAPI Proxy Server (ccproxy)                   │
│                             │                                       │
│  ┌──────────────────────────▼──────────────────────────────────┐   │
│  │  uvicorn (127.0.0.1:3001)                                   │   │
│  │                                                             │   │
│  │  ┌───────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │   │
│  │  │   Proxy   │  │ Dashboard│  │  Monitor │  │   Live   │   │   │
│  │  │  Router   │  │  (htmx)  │  │  /stats  │  │   SSE    │   │   │
│  │  └─────┬─────┘  └──────────┘  └──────────┘  └──────────┘  │   │
│  │        │                                                    │   │
│  │  ┌─────▼─────────────────────────────────────────────────┐  │   │
│  │  │  SubagentRouter → Provider.forward() → Upstream API   │  │   │
│  │  └──────────────────────────┬────────────────────────────┘  │   │
│  │                             │                               │   │
│  │  ┌──────────────────────────▼────────────────────────────┐  │   │
│  │  │  StorageService (SQLite WAL) ← stream_and_capture()   │  │   │
│  │  └───────────────────────────────────────────────────────┘  │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Communication Patterns

### Qt ↔ Proxy Thread

The proxy runs inside a `QThread` with its own `asyncio.run()` loop. Communication uses Qt signals:

| Signal | Direction | Purpose |
|--------|-----------|---------|
| `started_ok(int)` | Thread → GUI | Proxy is listening; payload is bound port |
| `failed(str)` | Thread → GUI | Startup or runtime failure |
| `stopped()` | Thread → GUI | Server fully torn down |
| `request_stop()` | GUI → Thread | Sets `server.should_exit = True` |

### GUI ↔ Proxy HTTP

Two widgets poll the proxy over HTTP (localhost):
- **ActivityTile**: `GET /api/stats` every 3 seconds
- **RecentActivityView**: `GET /api/requests` every 5 seconds

### Live Dashboard (SSE)

The embedded HTML dashboard uses Server-Sent Events (`GET /stream`) via an in-process `EventBus`. When the proxy captures a new request, it publishes to the bus, and connected SSE clients receive the event for live UI updates.

### Claude Code ↔ Proxy

Claude Code is launched as a PTY subprocess with `ANTHROPIC_BASE_URL` pointed at the proxy. All API calls from Claude Code flow through:

```
Claude Code CLI  →  POST /v1/messages  →  Proxy Router  →  Upstream Provider
                                                ↓
                                         SQLite Capture
                                                ↓
                                         EventBus (SSE)
                                                ↓
                                         Dashboard / ActivityTile
```

## Request Data Flow

```
1. Claude Code sends POST /v1/messages to http://127.0.0.1:3001
2. BodyCaptureMiddleware buffers the request body
3. AccessLogMiddleware logs the request line
4. proxy.py handler:
   a. Parse body as AnthropicRequest (Pydantic)
   b. Apply alias rewrite (e.g., "opus" → "claude-opus-4-8")
   c. SubagentRouter determines route:
      - Subagent detection (SHA256 of static prompt)
      - Endpoint mapping lookup
      - Provider pattern fallback (prefix-based)
   d. Marshal body for target (Anthropic passthrough or OpenAI conversion)
   e. Resolve target URL + API key from provider config
   f. Save RequestLog to SQLite (pre-response)
   g. provider.forward() → upstream HTTP call
   h. Capture response:
      - Non-streaming: read body, save ResponseLog
      - Streaming: yield SSE bytes while accumulating for storage
   i. Publish "new_request" event to EventBus
5. Response returned to Claude Code
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Separate asyncio loop in QThread | Qt and asyncio both want to own the event loop; isolation prevents subtle deadlocks |
| uvicorn signal handlers disabled | Qt owns process signals in a desktop app; uvicorn's handlers would race the GUI |
| HTTP polling (not direct EventBus) for widgets | Maintains clean boundary between layers; proxy could be extracted to standalone |
| SQLite WAL mode | Concurrent reads (dashboard) with single writer (proxy handler) without locking |
| Atomic file writes (tmp + os.replace) | Prevents corrupted profiles/credentials on crash mid-write |
| Sentinel API key (`sk-proxy-injected`) | Lets Claude Code launch without real auth; proxy rejects upstream calls until configured |
| `chmod 600` on credential files | Minimal POSIX security for plaintext key storage; keyring avoided for WSL2/headless compat |

## Directory Layout

```
src/
├── main.py                 # Entry point
├── config.py               # App constants
├── core/                   # Document model (editor tabs)
├── profiles/               # Profile domain (models, store, credentials, env)
├── proxy/                  # Proxy lifecycle (thread, config, paths, UI widgets)
├── ui/                     # All Qt widgets
│   ├── main_window.py      # Central orchestrator
│   ├── claude/             # Claude session launcher + profile editor
│   ├── terminal/           # PTY backend + pyte renderer
│   ├── widgets/            # Editor + preview
│   ├── components/         # Shared UI components
│   └── dialogs/            # Dialog windows
├── ccproxy/                # Vendored FastAPI proxy
│   └── app/
│       ├── main.py         # FastAPI factory
│       ├── config.py       # Pydantic Settings
│       ├── api/            # Route handlers
│       ├── providers/      # Upstream provider abstraction
│       ├── router/         # Subagent routing logic
│       ├── streaming/      # SSE stream capture
│       ├── db/             # SQLite storage
│       ├── live/           # EventBus (pub/sub)
│       ├── models/         # Pydantic DTOs
│       ├── conversations/  # JSONL reader
│       ├── middleware.py   # Body capture + access log
│       ├── utils/          # Header sanitization, ID gen
│       ├── templates/      # Jinja2 dashboard
│       └── static/         # CSS + JS
└── utils/                  # Shared utilities
```

## Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| GUI Framework | PySide6 (Qt6) | Desktop window, widgets, signals/slots |
| Web Framework | FastAPI + uvicorn | Embedded proxy server |
| Terminal Emulation | pyte | VT100/xterm screen state machine |
| Database | SQLite (aiosqlite) | Request/response capture |
| HTTP Client | httpx | Upstream provider calls + widget polling |
| Configuration | pydantic-settings | Layered config (YAML, env, dotenv) |
| Dashboard UI | Jinja2 + htmx + Alpine.js | Server-rendered reactive dashboard |
| Syntax Highlighting | Pygments | Editor widget |
| Data Display | pandas + tabulate | Table rendering in preview |
