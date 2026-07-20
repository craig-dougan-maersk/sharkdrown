# Pandas Markdown Editor - Sample Document

This document demonstrates the pandas table feature.

## Basic Table

Here's a regular markdown table:

| Feature | Status | Notes |
|---------|--------|-------|
| Markdown | ✓ | Supported |
| JSON | ✓ | Supported |
| YAML | ✓ | Supported |
| Pandas | ✓ | Interactive |

## Pandas DataFrame Example

You can embed pandas code that gets executed and rendered as a table:

```pandas
import pandas as pd

# Create a sample DataFrame
df = pd.DataFrame({
    'Name': ['Alice', 'Bob', 'Charlie', 'Diana'],
    'Age': [25, 30, 35, 28],
    'City': ['NYC', 'LA', 'SF', 'Chicago'],
    'Salary': [75000, 82000, 95000, 68000]
})
df
```

## Data Analysis

You can also perform calculations:

```pandas
import pandas as pd

df = pd.DataFrame({
    'Product': ['A', 'B', 'C', 'D'],
    'Sales': [120, 150, 90, 200],
    'Price': [10.5, 12.0, 8.5, 15.0]
})

# Calculate revenue
df['Revenue'] = df['Sales'] * df['Price']
df
```

## Statistics

```pandas
import pandas as pd
import numpy as np

# Generate random data
np.random.seed(42)
data = pd.DataFrame({
    'Group': ['X'] * 5 + ['Y'] * 5,
    'Value': np.random.randn(10) * 10 + 50
})

# Show summary statistics
data.groupby('Group')['Value'].describe()
```

## Conclusion

This editor supports all three formats with live preview and interactive pandas tables.
