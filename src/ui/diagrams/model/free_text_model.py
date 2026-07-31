from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class FreeTextModel:
    """Data model for a free-floating text annotation."""

    id: str = field(default_factory=lambda: uuid4().hex[:12])
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    font_size: int = 12
    color: str = "#000000"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "font_size": self.font_size,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FreeTextModel:
        return cls(
            id=data["id"],
            text=data.get("text", ""),
            x=data.get("x", 0.0),
            y=data.get("y", 0.0),
            font_size=data.get("font_size", 12),
            color=data.get("color", "#000000"),
        )
