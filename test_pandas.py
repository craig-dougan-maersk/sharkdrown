"""Test pandas table rendering in the preview widget."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.ui.widgets.preview_widget import PandasTableProcessor
from src.core.document import DocumentTab


def test_pandas_extraction():
    """Test that pandas code blocks are extracted correctly."""
    content = """# Test Document

Some text here.

```pandas
import pandas as pd
df = pd.DataFrame({
    'Name': ['Alice', 'Bob', 'Charlie'],
    'Age': [25, 30, 35]
})
df
```

More text.

```pandas
df2 = pd.DataFrame({'x': [1, 2, 3]})
df2
```
"""
    blocks = PandasTableProcessor.extract_pandas_blocks(content)
    assert len(blocks) == 2
    assert "import pandas as pd" in blocks[0]["code"]
    assert "df2" in blocks[1]["code"]
    print("✓ Pandas block extraction works")


def test_pandas_execution():
    """Test that pandas code executes and returns markdown table."""
    code = """
import pandas as pd
df = pd.DataFrame({
    'Name': ['Alice', 'Bob', 'Charlie'],
    'Age': [25, 30, 35]
})
df
"""
    result = PandasTableProcessor.execute_pandas_code(code)
    assert result is not None
    assert "Name" in result
    assert "Alice" in result
    assert "Bob" in result
    print("✓ Pandas code execution works")


def test_pandas_replacement():
    """Test that pandas blocks are replaced with tables in markdown."""
    content = """# Users

```pandas
import pandas as pd
df = pd.DataFrame({
    'Name': ['Alice', 'Bob'],
    'Age': [25, 30]
})
df
```
"""
    result = PandasTableProcessor.replace_pandas_blocks(content)
    assert "pandas" not in result
    assert "Name" in result
    assert "Alice" in result
    print("✓ Pandas block replacement works")


def test_pandas_error_handling():
    """Test that pandas errors are handled gracefully."""
    code = """
import pandas as pd
undefined_variable
"""
    result = PandasTableProcessor.execute_pandas_code(code)
    assert result is not None
    assert "Error" in result
    print("✓ Pandas error handling works")


def test_preview_rendering():
    """Test that preview widget renders markdown with pandas tables."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    from src.ui.widgets.preview_widget import PreviewWidget

    doc = DocumentTab()
    doc.content = """# Data

```pandas
import pandas as pd
df = pd.DataFrame({'A': [1, 2], 'B': [3, 4]})
df
```
"""
    preview = PreviewWidget()
    preview.set_document(doc)

    text = preview.preview.toPlainText()
    assert "A" in text
    assert "1" in text
    print("✓ Preview widget renders pandas tables")


if __name__ == "__main__":
    test_pandas_extraction()
    test_pandas_execution()
    test_pandas_replacement()
    test_pandas_error_handling()
    test_preview_rendering()
    print("\n✅ All pandas tests passed!")