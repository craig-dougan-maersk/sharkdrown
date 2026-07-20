# Pandas Markdown Editor

A desktop editor for Markdown, JSON, and YAML files with pandas table support.

## Features

- **Multi-format editing**: Markdown, JSON, YAML with syntax highlighting
- **Split view**: Editor and preview side-by-side or stacked
- **Tabbed interface**: Multiple files in tabs, movable between editor/preview sides
- **Pandas tables**: Write Python code in ` ```pandas ` blocks to render interactive tables
- **Live preview**: Real-time markdown rendering with formatted JSON/YAML display
- **Cross-platform**: Windows, macOS, and Linux

## Installation

```bash
pip install -r requirements.txt
```

## Usage

Run the editor:

```bash
python -m src.main
```

Or if installed:

```bash
markdown-editor
```

### Pandas Tables

Embed pandas code in markdown using fenced code blocks with the `pandas` language:

```markdown
```pandas
import pandas as pd

df = pd.DataFrame({
    'Name': ['Alice', 'Bob', 'Charlie'],
    'Age': [25, 30, 35],
    'City': ['NYC', 'LA', 'SF']
})
df
```
```

This renders as a markdown table in the preview:

| Name | Age | City |
|------|-----|------|
| Alice | 25 | NYC |
| Bob | 30 | LA |
| Charlie | 35 | SF |

## Keyboard Shortcuts

| Action | Shortcut |
|--------|----------|
| New file | Ctrl+N |
| Open file | Ctrl+O |
| Save | Ctrl+S |
| Save all | Ctrl+Shift+S |
| Close tab | Ctrl+W |
| Exit | Ctrl+Q |
| Toggle split view | Ctrl+D |
| Toggle preview | Ctrl+P |
| Side by side | Ctrl+1 |
| Stacked | Ctrl+2 |

## Architecture

```
src/
├── main.py              # Entry point
├── core/
│   └── document.py      # Document model
├── ui/
│   ├── main_window.py   # Main window with tabs and split view
│   └── widgets/
│       ├── editor_widget.py    # Editor with syntax highlighting
│       └── preview_widget.py   # Preview with pandas support
└── formats/             # Format handlers (TODO)
```

## Development

Run tests:

```bash
QT_QPA_PLATFORM=offscreen python test_app.py
QT_QPA_PLATFORM=offscreen python test_pandas.py
```

## License

MIT
