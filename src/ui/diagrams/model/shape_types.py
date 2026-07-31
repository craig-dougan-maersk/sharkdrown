from __future__ import annotations

from enum import Enum


class ShapeType(Enum):
    """Standard Mermaid flowchart node shapes with their bracket syntax."""

    RECTANGLE = "rectangle"
    ROUNDED_RECT = "rounded_rect"
    STADIUM = "stadium"
    SUBROUTINE = "subroutine"
    CYLINDRICAL = "cylindrical"
    CIRCLE = "circle"
    ASYMMETRIC = "asymmetric"
    RHOMBUS = "rhombus"
    HEXAGON = "hexagon"
    PARALLELOGRAM = "parallelogram"
    PARALLELOGRAM_ALT = "parallelogram_alt"
    TRAPEZOID = "trapezoid"
    TRAPEZOID_ALT = "trapezoid_alt"
    DOUBLE_CIRCLE = "double_circle"

    @property
    def brackets(self) -> tuple[str, str]:
        """Opening and closing bracket syntax for Mermaid node declaration."""
        return _BRACKET_MAP[self]

    @property
    def display_name(self) -> str:
        return _DISPLAY_NAMES[self]

    @property
    def category(self) -> str:
        return _CATEGORIES[self]


_BRACKET_MAP: dict[ShapeType, tuple[str, str]] = {
    ShapeType.RECTANGLE: ("[", "]"),
    ShapeType.ROUNDED_RECT: ("(", ")"),
    ShapeType.STADIUM: ("([", "])"),
    ShapeType.SUBROUTINE: ("[[", "]]"),
    ShapeType.CYLINDRICAL: ("[(", ")]"),
    ShapeType.CIRCLE: ("((", "))"),
    ShapeType.ASYMMETRIC: (">", "]"),
    ShapeType.RHOMBUS: ("{", "}"),
    ShapeType.HEXAGON: ("{{", "}}"),
    ShapeType.PARALLELOGRAM: ("[/", "/]"),
    ShapeType.PARALLELOGRAM_ALT: ("[\\", "\\]"),
    ShapeType.TRAPEZOID: ("[/", "\\]"),
    ShapeType.TRAPEZOID_ALT: ("[\\", "/]"),
    ShapeType.DOUBLE_CIRCLE: ("(((", ")))"),
}

_DISPLAY_NAMES: dict[ShapeType, str] = {
    ShapeType.RECTANGLE: "Rectangle",
    ShapeType.ROUNDED_RECT: "Rounded Rectangle",
    ShapeType.STADIUM: "Stadium",
    ShapeType.SUBROUTINE: "Subroutine",
    ShapeType.CYLINDRICAL: "Cylindrical (DB)",
    ShapeType.CIRCLE: "Circle",
    ShapeType.ASYMMETRIC: "Asymmetric",
    ShapeType.RHOMBUS: "Diamond",
    ShapeType.HEXAGON: "Hexagon",
    ShapeType.PARALLELOGRAM: "Parallelogram",
    ShapeType.PARALLELOGRAM_ALT: "Parallelogram Alt",
    ShapeType.TRAPEZOID: "Trapezoid",
    ShapeType.TRAPEZOID_ALT: "Trapezoid Alt",
    ShapeType.DOUBLE_CIRCLE: "Double Circle",
}

_CATEGORIES: dict[ShapeType, str] = {
    ShapeType.RECTANGLE: "Basic",
    ShapeType.ROUNDED_RECT: "Basic",
    ShapeType.CIRCLE: "Basic",
    ShapeType.DOUBLE_CIRCLE: "Basic",
    ShapeType.STADIUM: "Flowchart",
    ShapeType.RHOMBUS: "Flowchart",
    ShapeType.PARALLELOGRAM: "Flowchart",
    ShapeType.PARALLELOGRAM_ALT: "Flowchart",
    ShapeType.TRAPEZOID: "Flowchart",
    ShapeType.TRAPEZOID_ALT: "Flowchart",
    ShapeType.SUBROUTINE: "Process",
    ShapeType.CYLINDRICAL: "Process",
    ShapeType.HEXAGON: "Process",
    ShapeType.ASYMMETRIC: "Process",
}
