"""Tests for Mermaid diagram model, code generation, and file I/O round-trip."""

from __future__ import annotations

import json
from pathlib import Path

from src.ui.diagrams.codegen.mermaid_generator import MermaidCodeGenerator
from src.ui.diagrams.io.file_io import layout_path_for, load_diagram, save_diagram
from src.ui.diagrams.model import (
    ConnectorModel,
    DiagramDocument,
    FreeTextModel,
    NodeModel,
    ShapeType,
)
from src.ui.diagrams.model.connector_types import (
    EndingType,
    LineStyle,
    build_arrow_syntax,
)


class TestShapeTypes:
    def test_bracket_syntax(self):
        assert ShapeType.RECTANGLE.brackets == ("[", "]")
        assert ShapeType.CIRCLE.brackets == ("((", "))")
        assert ShapeType.RHOMBUS.brackets == ("{", "}")
        assert ShapeType.STADIUM.brackets == ("([", "])")

    def test_all_shapes_have_brackets(self):
        for shape in ShapeType:
            open_br, close_br = shape.brackets
            assert isinstance(open_br, str) and len(open_br) > 0
            assert isinstance(close_br, str) and len(close_br) > 0

    def test_categories(self):
        assert ShapeType.RECTANGLE.category == "Basic"
        assert ShapeType.RHOMBUS.category == "Flowchart"
        assert ShapeType.SUBROUTINE.category == "Process"


class TestConnectorTypes:
    def test_basic_arrow(self):
        result = build_arrow_syntax(LineStyle.SOLID, EndingType.OPEN, EndingType.ARROW)
        assert result == "-->"

    def test_dotted_arrow(self):
        result = build_arrow_syntax(
            LineStyle.DOTTED, EndingType.OPEN, EndingType.ARROW
        )
        assert result == "-.->", f"Got: {result}"

    def test_thick_arrow(self):
        result = build_arrow_syntax(LineStyle.THICK, EndingType.OPEN, EndingType.ARROW)
        assert result == "==>"

    def test_bidirectional(self):
        result = build_arrow_syntax(
            LineStyle.SOLID, EndingType.ARROW, EndingType.ARROW
        )
        assert result == "<-->"

    def test_with_label(self):
        result = build_arrow_syntax(
            LineStyle.SOLID, EndingType.OPEN, EndingType.ARROW, "yes"
        )
        assert result == "--|yes|>"

    def test_circle_ending(self):
        result = build_arrow_syntax(
            LineStyle.SOLID, EndingType.OPEN, EndingType.CIRCLE
        )
        assert result == "--o"

    def test_cross_ending(self):
        result = build_arrow_syntax(LineStyle.SOLID, EndingType.OPEN, EndingType.CROSS)
        assert result == "--x"


class TestNodeModel:
    def test_round_trip(self):
        node = NodeModel(
            id="abc123",
            shape_type=ShapeType.HEXAGON,
            label="Process",
            x=100.0,
            y=200.0,
            width=140.0,
            height=80.0,
            fill_color="#ff0000",
            outline_color="#000000",
        )
        data = node.to_dict()
        restored = NodeModel.from_dict(data)
        assert restored.id == "abc123"
        assert restored.shape_type == ShapeType.HEXAGON
        assert restored.label == "Process"
        assert restored.x == 100.0
        assert restored.fill_color == "#ff0000"


class TestDiagramDocument:
    def test_add_and_remove_node(self):
        doc = DiagramDocument()
        node = NodeModel(id="n1", label="Start")
        doc.add_node(node)
        assert len(doc.nodes) == 1
        doc.remove_node("n1")
        assert len(doc.nodes) == 0

    def test_remove_node_cascades_connectors(self):
        doc = DiagramDocument()
        doc.add_node(NodeModel(id="n1"))
        doc.add_node(NodeModel(id="n2"))
        doc.add_connector(ConnectorModel(id="c1", source_id="n1", target_id="n2"))
        doc.remove_node("n1")
        assert len(doc.connectors) == 0

    def test_serialization_round_trip(self):
        doc = DiagramDocument(direction="LR")
        doc.add_node(NodeModel(id="n1", shape_type=ShapeType.CIRCLE, label="A"))
        doc.add_node(NodeModel(id="n2", shape_type=ShapeType.RECTANGLE, label="B"))
        doc.add_connector(
            ConnectorModel(id="c1", source_id="n1", target_id="n2", label="flow")
        )
        doc.add_free_text(FreeTextModel(id="ft1", text="Note", x=50, y=50))

        data = doc.to_dict()
        restored = DiagramDocument.from_dict(data)
        assert restored.direction == "LR"
        assert len(restored.nodes) == 2
        assert restored.nodes[0].shape_type == ShapeType.CIRCLE
        assert len(restored.connectors) == 1
        assert restored.connectors[0].label == "flow"
        assert len(restored.free_texts) == 1
        assert restored.free_texts[0].text == "Note"


class TestMermaidCodeGenerator:
    def test_simple_flowchart(self):
        doc = DiagramDocument()
        doc.add_node(NodeModel(id="n1", shape_type=ShapeType.RECTANGLE, label="Start"))
        doc.add_node(NodeModel(id="n2", shape_type=ShapeType.RHOMBUS, label="Decision"))
        doc.add_connector(
            ConnectorModel(id="c1", source_id="n1", target_id="n2", label="go")
        )

        gen = MermaidCodeGenerator()
        output = gen.generate(doc)

        assert output.startswith("flowchart TD\n")
        assert "A[Start]" in output
        assert "B{Decision}" in output
        assert "A --|go|> B" in output

    def test_all_shape_types_generate(self):
        doc = DiagramDocument()
        for i, shape in enumerate(ShapeType):
            doc.add_node(NodeModel(id=f"n{i}", shape_type=shape, label=f"S{i}"))

        gen = MermaidCodeGenerator()
        output = gen.generate(doc)
        assert "flowchart TD" in output
        assert len(output.strip().split("\n")) == 1 + len(ShapeType)

    def test_label_generation_wraps(self):
        gen = MermaidCodeGenerator()
        assert gen._index_to_label(0) == "A"
        assert gen._index_to_label(25) == "Z"
        assert gen._index_to_label(26) == "AA"
        assert gen._index_to_label(27) == "AB"

    def test_direction_lr(self):
        doc = DiagramDocument(direction="LR")
        doc.add_node(NodeModel(id="n1", label="X"))
        gen = MermaidCodeGenerator()
        output = gen.generate(doc)
        assert output.startswith("flowchart LR\n")


class TestFileIO:
    def test_save_and_load_round_trip(self, tmp_path: Path):
        mmd_path = tmp_path / "test.mmd"
        doc = DiagramDocument(direction="LR")
        doc.add_node(
            NodeModel(id="n1", shape_type=ShapeType.STADIUM, label="Hello", x=50, y=75)
        )
        doc.add_node(NodeModel(id="n2", shape_type=ShapeType.CIRCLE, label="World"))
        doc.add_connector(ConnectorModel(id="c1", source_id="n1", target_id="n2"))

        save_diagram(mmd_path, doc)

        assert mmd_path.exists()
        assert layout_path_for(mmd_path).exists()

        mermaid_text = mmd_path.read_text(encoding="utf-8")
        assert "flowchart LR" in mermaid_text
        assert "Hello" in mermaid_text

        layout_data = json.loads(
            layout_path_for(mmd_path).read_text(encoding="utf-8")
        )
        assert layout_data["version"] == 1

        restored = load_diagram(mmd_path)
        assert restored.direction == "LR"
        assert len(restored.nodes) == 2
        assert restored.nodes[0].x == 50
        assert restored.nodes[0].y == 75
        assert restored.nodes[0].shape_type == ShapeType.STADIUM

    def test_load_without_layout_returns_empty(self, tmp_path: Path):
        mmd_path = tmp_path / "bare.mmd"
        mmd_path.write_text("flowchart TD\n    A[hello]\n", encoding="utf-8")
        doc = load_diagram(mmd_path)
        assert len(doc.nodes) == 0
