# SharkDrown — Feature Reference

## Document Editor

| Feature | Description |
|---------|-------------|
| Multi-format support | Markdown, JSON, YAML, Plain Text |
| New file dialog | Type picker (Ctrl+N) with starter content templates |
| Syntax highlighting | Format-aware highlighting (headers, strings, keys, etc.) |
| Markdown toolbar | H1/H2/H3, Bold, Italic, Bullet/Numbered lists, Links, Tables |
| Built-in linting | JSON (parse errors), YAML (parse errors), Markdown (style rules) |
| Lint panel | Toggleable panel (Ctrl+Shift+L) showing errors with click-to-navigate |
| Split view | Editor + preview side-by-side or stacked |
| Live preview | Real-time Markdown rendering with formatted JSON/YAML |
| Pandas tables | Execute Python code in `\`\`\`pandas` blocks to render DataFrames |
| File type detection | Automatic format detection from file extension on open |
| Format override | Manual format selection via status bar dropdown |
| Coloured tab icons | File-type-specific coloured dot on each tab |
| Tab rename | Double-click tab title to rename |
| Tab reordering | Drag tabs to reorder |
| Unsaved changes guard | Prompt on close if modifications exist (Save/Discard/Cancel) |
| Cursor memory | Each tab remembers its cursor position independently |
| Status bar stats | Word count, line count, cursor position (Ln, Col) |
| Font size control | Ctrl+=/- to adjust, slider in status bar, 8–32pt range |

## Terminal Emulator

| Feature | Description |
|---------|-------------|
| Real PTY | Fork/exec with actual pseudo-terminal (not simulated) |
| pyte rendering | Full VT100/xterm-256color terminal emulation |
| Multiple sessions | Tabbed interface with closable, movable tabs |
| Terminal themes | Dark, Solarized Dark, Solarized Light, Monokai, Nord |
| Font settings | Configurable family (monospace fonts) and size |
| Default shell | Configurable shell path (Terminal Settings) |
| Environment vars | Custom environment variables per session |
| Scrollback | 5000 lines of history, scroll up/down |
| Text selection | Linear selection (default), block/column selection (Alt/Cmd held) |
| Copy/Paste | Standard keyboard shortcuts |
| Auto-close on exit | Tab closes automatically when shell process exits |
| Session cycling | Alt+Left/Right to switch between sessions |
| Session logging | Plain-text logs written to ~/.sharkdrown/logs/ |
| Session recording | Asciicast v2 format for replay |
| oh-my-posh support | Compatible with prompt frameworks (TERM=xterm-256color) |
| Terminal Settings | Dedicated settings dialog (theme, font, shell, env vars) |

## Claude Sessions

| Feature | Description |
|---------|-------------|
| Managed sessions | Launch Claude Code with automatic proxy configuration |
| Profile system | Named profiles with provider/credential/routing configuration |
| Multi-provider | Route to Anthropic, OpenAI-compatible, MiniMax, OpenRouter, etc. |
| Model aliasing | Transparent model name remapping across providers |
| Subagent routing | Detect SHA256-hashed model IDs and route separately |
| Credential management | Secure API key storage per provider |
| Session environment | Profile-driven environment variable injection |

## Proxy Engine

| Feature | Description |
|---------|-------------|
| Embedded FastAPI | Runs in a QThread with asyncio event loop |
| Anthropic provider | Native pass-through with hop-by-hop header handling |
| OpenAI provider | Full Anthropic ↔ OpenAI protocol conversion |
| Subagent router | Priority-based routing: subagent → endpoint map → default → prefix |
| Streaming capture | SSE stream-through with full response accumulation |
| SQLite storage | WAL-mode database for request/response history |
| Token tracking | Input/output/total token counting per request |
| Activity tile | Real-time traffic indicator in status bar |
| Auto-start | Optional automatic proxy launch on app start |
| Dashboard API | RESTful endpoints for request history and monitoring |
| Dual-bucket rate limiting | Separate token buckets for LLM paths and admin/replay paths |
| Sensitive header sanitization | SHA-256 redaction of Authorization, Cookie, X-Auth-Token, and related headers before storage |
| SSRF prevention | `ProviderCfg.url` restricted to HTTP/HTTPS via Pydantic `HttpUrl` validation at config load |
| Replay safety guard | Replay endpoint rejects SHA-256-redacted API keys with a clear error instead of forwarding them |
| Path traversal prevention | Conversations API validates `project`/`session_id` against base directory before opening files |
| Configurable upstream timeouts | `settings.server.timeouts.read` applied to all provider HTTP clients |

## Application Chrome

| Feature | Description |
|---------|-------------|
| Three-tab workspace | Documents / Terminals / Claude top-level tabs |
| Theme system | Dark, Light, System-follow modes |
| UI font control | Adjustable UI chrome font (family + size) |
| Content font control | Independent font sizing for editor/terminal content |
| Persistent settings | QSettings-backed persistence across sessions |
| Cross-platform | macOS, Linux, Windows (PySide6/Qt6) |
