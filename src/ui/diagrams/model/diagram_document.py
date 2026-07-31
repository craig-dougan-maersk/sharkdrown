from __future__ import annotations

from dataclasses import dataclass, field

from .connector_model import ConnectorModel
from .free_text_model import FreeTextModel
from .node_model import NodeModel


@dataclass
class DiagramDocument:
    """Aggregate root for a Mermaid diagram's visual model.

    Pure data — no Qt dependency. Serializable to/from dict for JSON persistence.
    The scene reads from this on load and writes back on every user action.
    """

    nodes: list[NodeModel] = field(default_factory=list)
    connectors: list[ConnectorModel] = field(default_factory=list)
    free_texts: list[FreeTextModel] = field(default_factory=list)
    direction: str = "TD"

    def add_node(self, node: NodeModel) -> None:
        self.nodes.append(node)

    def remove_node(self, node_id: str) -> None:
        self.nodes = [n for n in self.nodes if n.id != node_id]
        self.connectors = [
            c
            for c in self.connectors
            if c.source_id != node_id and c.target_id != node_id
        ]

    def get_node(self, node_id: str) -> NodeModel | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def add_connector(self, connector: ConnectorModel) -> None:
        self.connectors.append(connector)

    def remove_connector(self, connector_id: str) -> None:
        self.connectors = [c for c in self.connectors if c.id != connector_id]

    def add_free_text(self, text: FreeTextModel) -> None:
        self.free_texts.append(text)

    def remove_free_text(self, text_id: str) -> None:
        self.free_texts = [t for t in self.free_texts if t.id != text_id]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "direction": self.direction,
            "nodes": [n.to_dict() for n in self.nodes],
            "connectors": [c.to_dict() for c in self.connectors],
            "free_texts": [t.to_dict() for t in self.free_texts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiagramDocument:
        return cls(
            direction=data.get("direction", "TD"),
            nodes=[NodeModel.from_dict(n) for n in data.get("nodes", [])],
            connectors=[
                ConnectorModel.from_dict(c) for c in data.get("connectors", [])
            ],
            free_texts=[
                FreeTextModel.from_dict(t) for t in data.get("free_texts", [])
            ],
        )
