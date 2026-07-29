# Changelog

All notable changes to SharkDrown are documented in this file.

## [Unreleased]

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
