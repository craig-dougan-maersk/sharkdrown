# SharkDrown

A PySide6 desktop IDE for managing Claude Code sessions with an embedded multi-provider proxy, integrated terminal emulator, and multi-format document editor.

## Overview

SharkDrown combines three workspaces in a single application:

1. **Documents** — A tabbed editor for Markdown, JSON, YAML, and plain text with live preview, syntax highlighting, linting, and pandas table rendering.
2. **Terminals** — A full terminal emulator with PTY support, theming, session logging, and oh-my-posh compatibility.
3. **Claude** — Managed Claude Code sessions routed through an embedded proxy that enables multi-provider routing, model aliasing, and request capture.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python -m src.main
```

## Workspaces

### Documents

- Multi-format editing: Markdown, JSON, YAML, Plain Text
- Syntax highlighting for all supported formats
- Markdown toolbar (headings, bold, italic, lists, links, tables)
- Built-in linting (JSON validation, YAML validation, Markdown style checks)
- Split view with live preview (side-by-side or stacked)
- Pandas table rendering in Markdown preview (`\`\`\`pandas` code blocks)
- File type selection dialog on new file creation
- Coloured tab icons indicating file type
- Double-click tab to rename
- Unsaved changes guard on close
- Per-tab cursor position memory
- Status bar: word count, line count, cursor position (Ln/Col)

### Terminals

- Real PTY-backed terminal emulator (pyte + pty.fork)
- Multiple concurrent sessions in tabs
- Terminal-specific themes (Dark, Solarized Dark/Light, Monokai, Nord)
- Configurable font family and size
- Configurable default shell and environment variables
- Text selection (linear by default, block with Alt/Cmd held)
- Scrollback history (5000 lines)
- Session recording (asciicast v2 format)
- Plain-text session logging (~/.sharkdrown/logs/)
- Auto-close tab on shell exit
- Alt+Left/Right to cycle between sessions
- Copy/Paste support

### Claude Sessions

- Launch Claude Code sessions with proxy routing
- Multi-provider support (Anthropic, OpenAI-compatible, MiniMax, OpenRouter, etc.)
- Profile system for managing credentials and routing rules
- Model aliasing (e.g. route "claude-sonnet-4" to a different provider)
- Subagent-aware routing (SHA256 model ID detection)
- Request/response capture to SQLite
- Activity tile showing live proxy traffic
- Auto-start proxy option

## Proxy System

The embedded proxy intercepts Claude Code API traffic and provides:

- **Provider routing** — Route requests to Anthropic, OpenAI-compatible endpoints, or custom providers
- **Model aliasing** — Map model names to different providers transparently
- **Subagent detection** — Identify and route subagent requests separately
- **Endpoint mapping** — Per-path routing rules (e.g. /v1/messages → provider A, /v1/complete → provider B)
- **Protocol conversion** — Automatic Anthropic ↔ OpenAI format translation
- **Streaming capture** — Full request/response logging without breaking SSE streams
- **Token tracking** — Input/output/total token counting per request

## Keyboard Shortcuts

| Action | Shortcut |
|--------|----------|
| New file (with type picker) | Ctrl+N |
| Open file | Ctrl+O |
| Save | Ctrl+S |
| Save all | Ctrl+Shift+S |
| Close tab | Ctrl+W |
| Exit | Ctrl+Q |
| Toggle split view | Ctrl+D |
| Toggle preview | Ctrl+P |
| Side by side | Ctrl+1 |
| Stacked | Ctrl+2 |
| Toggle lint panel | Ctrl+Shift+L |
| Increase content font | Ctrl+= |
| Decrease content font | Ctrl+- |
| Reset content font | Ctrl+0 |
| New terminal session | Ctrl+Alt+T |
| Focus terminals tab | Ctrl+Alt+Shift+T |
| Previous terminal session | Alt+Left |
| Next terminal session | Alt+Right |
| Copy (terminal) | Ctrl+C / Cmd+C |
| Paste (terminal) | Ctrl+V / Cmd+V |

## Architecture

```
src/
├── main.py                    # Entry point
├── config.py                  # App title, name, org
├── core/
│   └── document.py            # Document model (format, cursor, content)
├── profiles/                  # Profile/credential management
│   ├── store.py               # ProfileStore — CRUD for routing profiles
│   ├── models.py              # Profile, Provider, Endpoint models
│   ├── credential_store.py    # Secure credential storage
│   └── env_builder.py         # Build env vars for Claude sessions
├── proxy/                     # Proxy UI integration
│   ├── server_thread.py       # QThread hosting the FastAPI proxy
│   ├── proxy_config.py        # Proxy settings (port, autostart)
│   ├── activity_tile.py       # Status bar traffic indicator
│   └── settings_dialog.py     # Proxy configuration dialog
├── ccproxy/                   # Embedded FastAPI proxy engine
│   └── app/
│       ├── main.py            # FastAPI app factory
│       ├── api/               # HTTP endpoints (proxy, dashboard, monitor)
│       ├── providers/         # Provider abstraction (Anthropic, OpenAI)
│       ├── router/            # Subagent-aware routing pipeline
│       ├── streaming/         # SSE stream capture
│       ├── db/                # SQLite storage (aiosqlite, WAL mode)
│       └── models/            # Pydantic models
├── ui/
│   ├── main_window/           # MainWindow + mixins
│   │   ├── window.py          # Central widget, lifecycle
│   │   ├── _document_ops.py   # Tab management, file I/O, lint
│   │   ├── _menu_construction.py  # Menus, status bar
│   │   ├── _profile_management.py # Profile CRUD UI
│   │   └── _proxy_lifecycle.py    # Start/stop/restart proxy
│   ├── widgets/
│   │   ├── editor_widget.py   # QTextEdit + syntax highlighters
│   │   ├── preview_widget.py  # Live preview + pandas tables
│   │   ├── markdown_toolbar.py # Formatting toolbar
│   │   └── lint_panel.py      # Lint results + built-in linters
│   ├── terminal/
│   │   ├── terminal_container_view.py  # Tab container for sessions
│   │   ├── terminal_session_view.py    # PTY + pyte rendering
│   │   ├── pty_backend.py     # PTY fork/exec + I/O thread
│   │   ├── renderer.py        # pyte Screen → HTML
│   │   ├── settings_dialog.py # Terminal settings (theme, font, shell)
│   │   ├── session_logger.py  # Plain-text session logging
│   │   └── session_recorder.py # Asciicast v2 recording
│   ├── claude/
│   │   ├── claude_container_view.py  # Claude session management
│   │   └── profile_editor/    # Multi-tab profile editor dialog
│   ├── dialogs/
│   │   └── new_file_dialog.py # New file type picker
│   └── theme.py               # Application theme (dark/light/system)
└── formats/                   # Format handlers (extensible)
```

## Development

Run tests:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest test_terminal.py -q
```

## License

MIT
