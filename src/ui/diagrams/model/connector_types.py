from __future__ import annotations

from enum import Enum


class LineStyle(Enum):
    """Connector line styles mapped to Mermaid arrow syntax."""

    SOLID = "solid"
    DOTTED = "dotted"
    THICK = "thick"

    @property
    def mermaid_line(self) -> str:
        """The line portion of the arrow syntax (between endings)."""
        return _LINE_SYNTAX[self]


class EndingType(Enum):
    """Connector endpoint types."""

    ARROW = "arrow"
    OPEN = "open"
    CIRCLE = "circle"
    CROSS = "cross"

    @property
    def mermaid_right(self) -> str:
        """Right-side ending syntax."""
        return _RIGHT_ENDING[self]

    @property
    def mermaid_left(self) -> str:
        """Left-side ending syntax."""
        return _LEFT_ENDING[self]


_LINE_SYNTAX: dict[LineStyle, str] = {
    LineStyle.SOLID: "--",
    LineStyle.DOTTED: "-.-",
    LineStyle.THICK: "==",
}

_RIGHT_ENDING: dict[EndingType, str] = {
    EndingType.ARROW: ">",
    EndingType.OPEN: "-",
    EndingType.CIRCLE: "o",
    EndingType.CROSS: "x",
}

_LEFT_ENDING: dict[EndingType, str] = {
    EndingType.ARROW: "<",
    EndingType.OPEN: "-",
    EndingType.CIRCLE: "o",
    EndingType.CROSS: "x",
}


def build_arrow_syntax(
    line_style: LineStyle,
    start_ending: EndingType,
    end_ending: EndingType,
    label: str = "",
) -> str:
    """Build the full Mermaid arrow syntax between two nodes.

    Examples:
        -->     solid arrow
        -.->    dotted arrow
        ==>     thick arrow
        --o     solid circle ending
        <-->    bidirectional arrows
        -->|text|  arrow with label
    """
    line = line_style.mermaid_line
    right = end_ending.mermaid_right

    left_prefix = ""
    if start_ending != EndingType.OPEN:
        left_prefix = start_ending.mermaid_left

    if label:
        return f"{left_prefix}{line}|{label}|{right}"
    return f"{left_prefix}{line}{right}"
