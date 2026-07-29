"""Lint panel — shows validation errors for JSON, YAML, and Markdown."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass
class LintIssue:
    line: int
    col: int
    severity: str
    message: str


class LintPanel(QWidget):
    """Displays lint issues in a table. Emits ``issue_clicked(line, col)``."""

    issue_clicked = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Line", "Col", "Severity", "Message"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self._table)

        self._issues: list[LintIssue] = []

    def set_issues(self, issues: list[LintIssue]) -> None:
        self._issues = issues
        self._table.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            self._table.setItem(row, 0, QTableWidgetItem(str(issue.line)))
            self._table.setItem(row, 1, QTableWidgetItem(str(issue.col)))
            self._table.setItem(row, 2, QTableWidgetItem(issue.severity))
            self._table.setItem(row, 3, QTableWidgetItem(issue.message))

    def clear_issues(self) -> None:
        self._issues = []
        self._table.setRowCount(0)

    def issue_count(self) -> int:
        return len(self._issues)

    def _on_cell_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._issues):
            issue = self._issues[row]
            self.issue_clicked.emit(issue.line, issue.col)


# ---------------------------------------------------------------------------
# Built-in linters
# ---------------------------------------------------------------------------


def lint_json(text: str) -> list[LintIssue]:
    """Validate JSON and return any parse errors."""
    if not text.strip():
        return []
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        return [LintIssue(line=e.lineno, col=e.colno, severity="error", message=e.msg)]
    return []


def lint_yaml(text: str) -> list[LintIssue]:
    """Validate YAML and return any parse errors."""
    if not text.strip():
        return []
    try:
        import yaml
        yaml.safe_load(text)
    except ImportError:
        return []
    except yaml.YAMLError as e:
        if hasattr(e, "problem_mark") and e.problem_mark is not None:
            mark = e.problem_mark
            msg = getattr(e, "problem", str(e)) or str(e)
            return [LintIssue(
                line=mark.line + 1,
                col=mark.column + 1,
                severity="error",
                message=msg,
            )]
        return [LintIssue(line=1, col=1, severity="error", message=str(e))]
    return []


def lint_markdown(text: str) -> list[LintIssue]:
    """Basic markdown lint rules."""
    issues: list[LintIssue] = []
    lines = text.split("\n")
    prev_blank = False
    last_heading_level = 0

    for i, line in enumerate(lines, start=1):
        # Trailing whitespace
        if line != line.rstrip() and line.strip():
            issues.append(LintIssue(
                line=i, col=len(line.rstrip()) + 1,
                severity="warning", message="Trailing whitespace",
            ))

        # Multiple consecutive blank lines
        is_blank = not line.strip()
        if is_blank and prev_blank:
            issues.append(LintIssue(
                line=i, col=1,
                severity="warning", message="Multiple consecutive blank lines",
            ))
        prev_blank = is_blank

        # Heading level skip (e.g. # then ###)
        m = re.match(r"^(#{1,6})\s", line)
        if m:
            level = len(m.group(1))
            if last_heading_level > 0 and level > last_heading_level + 1:
                issues.append(LintIssue(
                    line=i, col=1,
                    severity="warning",
                    message=f"Heading level skipped (H{last_heading_level} → H{level})",
                ))
            last_heading_level = level

    return issues


def lint_text(text: str, format_type: str) -> list[LintIssue]:
    """Dispatch to the appropriate linter based on format."""
    if format_type == "json":
        return lint_json(text)
    elif format_type == "yaml":
        return lint_yaml(text)
    elif format_type == "markdown":
        return lint_markdown(text)
    return []
