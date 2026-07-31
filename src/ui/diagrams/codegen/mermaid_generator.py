from __future__ import annotations

from ..model.connector_types import build_arrow_syntax
from ..model.diagram_document import DiagramDocument


class MermaidCodeGenerator:
    """Generates Mermaid flowchart syntax from a DiagramDocument."""

    def generate(self, doc: DiagramDocument) -> str:
        lines: list[str] = []
        lines.append(f"flowchart {doc.direction}")

        id_map = self._build_id_map(doc)

        for node in doc.nodes:
            short_id = id_map[node.id]
            open_br, close_br = node.shape_type.brackets
            label = node.label or short_id
            lines.append(f"    {short_id}{open_br}{label}{close_br}")

        if doc.connectors:
            lines.append("")

        for conn in doc.connectors:
            src = id_map.get(conn.source_id, conn.source_id)
            tgt = id_map.get(conn.target_id, conn.target_id)
            arrow = build_arrow_syntax(
                conn.line_style, conn.start_ending, conn.end_ending, conn.label
            )
            lines.append(f"    {src} {arrow} {tgt}")

        return "\n".join(lines) + "\n"

    def _build_id_map(self, doc: DiagramDocument) -> dict[str, str]:
        """Map internal UUIDs to short alphabetic IDs (A, B, ..., AA, AB, ...)."""
        id_map: dict[str, str] = {}
        for i, node in enumerate(doc.nodes):
            id_map[node.id] = self._index_to_label(i)
        return id_map

    def _index_to_label(self, index: int) -> str:
        """Convert zero-based index to spreadsheet-style label (A-Z, AA-AZ, ...)."""
        result = ""
        n = index
        while True:
            result = chr(ord("A") + n % 26) + result
            n = n // 26 - 1
            if n < 0:
                break
        return result
