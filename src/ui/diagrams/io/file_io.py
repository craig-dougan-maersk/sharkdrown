from __future__ import annotations

import json
from pathlib import Path

from ..codegen.mermaid_generator import MermaidCodeGenerator
from ..model.diagram_document import DiagramDocument


def layout_path_for(mmd_path: Path) -> Path:
    """Return the sidecar layout JSON path for a given .mmd file."""
    return mmd_path.with_suffix(".mmd.layout.json")


def save_diagram(path: Path, doc: DiagramDocument) -> None:
    """Save a diagram as .mmd (generated Mermaid) + .mmd.layout.json (positions)."""
    generator = MermaidCodeGenerator()
    mermaid_text = generator.generate(doc)
    path.write_text(mermaid_text, encoding="utf-8")

    layout_data = doc.to_dict()
    layout_file = layout_path_for(path)
    layout_file.write_text(
        json.dumps(layout_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_diagram(path: Path) -> DiagramDocument:
    """Load a diagram from its layout sidecar. Falls back to empty doc if missing."""
    layout_file = layout_path_for(path)
    if layout_file.exists():
        data = json.loads(layout_file.read_text(encoding="utf-8"))
        return DiagramDocument.from_dict(data)
    return DiagramDocument()
