from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from .connector_types import EndingType, LineStyle


@dataclass
class ConnectorModel:
    """Data model for a connector (edge) between two nodes."""

    id: str = field(default_factory=lambda: uuid4().hex[:12])
    source_id: str = ""
    target_id: str = ""
    line_style: LineStyle = LineStyle.SOLID
    start_ending: EndingType = EndingType.OPEN
    end_ending: EndingType = EndingType.ARROW
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "line_style": self.line_style.value,
            "start_ending": self.start_ending.value,
            "end_ending": self.end_ending.value,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConnectorModel:
        return cls(
            id=data["id"],
            source_id=data["source_id"],
            target_id=data["target_id"],
            line_style=LineStyle(data.get("line_style", "solid")),
            start_ending=EndingType(data.get("start_ending", "open")),
            end_ending=EndingType(data.get("end_ending", "arrow")),
            label=data.get("label", ""),
        )
