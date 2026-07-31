from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from .shape_types import ShapeType


@dataclass
class NodeModel:
    """Data model for a single diagram node (shape)."""

    id: str = field(default_factory=lambda: uuid4().hex[:12])
    shape_type: ShapeType = ShapeType.RECTANGLE
    label: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 120.0
    height: float = 60.0
    fill_color: str = "#ffffff"
    outline_color: str = "#333333"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "shape_type": self.shape_type.value,
            "label": self.label,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "fill_color": self.fill_color,
            "outline_color": self.outline_color,
        }

    @classmethod
    def from_dict(cls, data: dict) -> NodeModel:
        return cls(
            id=data["id"],
            shape_type=ShapeType(data["shape_type"]),
            label=data.get("label", ""),
            x=data.get("x", 0.0),
            y=data.get("y", 0.0),
            width=data.get("width", 120.0),
            height=data.get("height", 60.0),
            fill_color=data.get("fill_color", "#ffffff"),
            outline_color=data.get("outline_color", "#333333"),
        )
