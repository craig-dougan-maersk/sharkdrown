"""Tests for the interactive connector functionality added to the diagram editor."""

from __future__ import annotations

from PySide6.QtCore import QPointF

from src.ui.diagrams.model.connector_model import ConnectorModel
from src.ui.diagrams.model.connector_types import EndingType, LineStyle
from src.ui.diagrams.model.diagram_document import DiagramDocument
from src.ui.diagrams.model.node_model import NodeModel
from src.ui.diagrams.model.shape_types import ShapeType
from src.ui.diagrams.scene.diagram_scene import DiagramScene
from src.ui.diagrams.scene.items.connector_item import ConnectorItem
from src.ui.diagrams.scene.items.node_item import DiagramNodeItem


class TestConnectorModelExtensions:
    def test_color_and_weight_defaults(self):
        c = ConnectorModel()
        assert c.color == "#555555"
        assert c.weight == 2.0

    def test_round_trip_preserves_color_and_weight(self):
        c = ConnectorModel(
            source_id="a", target_id="b", color="#ff00aa", weight=4.5
        )
        d = c.to_dict()
        assert d["color"] == "#ff00aa"
        assert d["weight"] == 4.5
        c2 = ConnectorModel.from_dict(d)
        assert c2.color == "#ff00aa"
        assert c2.weight == 4.5

    def test_legacy_dict_uses_defaults(self):
        legacy = {
            "id": "x",
            "source_id": "a",
            "target_id": "b",
            "line_style": "solid",
            "start_ending": "open",
            "end_ending": "arrow",
            "label": "",
        }
        c = ConnectorModel.from_dict(legacy)
        assert c.color == "#555555"
        assert c.weight == 2.0


class TestDocumentMutation:
    def test_update_connector(self):
        doc = DiagramDocument()
        a = NodeModel(id="a")
        b = NodeModel(id="b")
        doc.add_node(a)
        doc.add_node(b)
        c = ConnectorModel(id="c1", source_id="a", target_id="b")
        doc.add_connector(c)

        c.label = "new"
        doc.update_connector(c)

        assert doc.get_connector("c1") is c
        assert doc.get_connector("c1").label == "new"

    def test_update_connector_missing_is_noop(self):
        doc = DiagramDocument()
        c = ConnectorModel(id="ghost", source_id="a", target_id="b")
        doc.update_connector(c)  # should not raise
        assert doc.get_connector("ghost") is None

    def test_get_connector(self):
        doc = DiagramDocument()
        c = ConnectorModel(id="c1", source_id="a", target_id="b")
        doc.add_connector(c)
        assert doc.get_connector("c1") is c
        assert doc.get_connector("nope") is None


class TestAnchorPoint:
    def test_ensure_anchors_idempotent(self, qapp):
        node = DiagramNodeItem(NodeModel(id="n1", shape_type=ShapeType.RECTANGLE))
        anchors1 = node.ensure_connection_anchors()
        anchors2 = node.ensure_connection_anchors()
        assert anchors1 is anchors2
        assert len(anchors1) == 4

    def test_anchors_hidden_by_default(self, qapp):
        node = DiagramNodeItem(NodeModel(id="n1", shape_type=ShapeType.RECTANGLE))
        anchors = node.ensure_connection_anchors()
        for a in anchors:
            assert not a.isVisible()

    def test_show_and_hide_anchors(self, qapp):
        node = DiagramNodeItem(NodeModel(id="n1", shape_type=ShapeType.RECTANGLE))
        node.show_connection_anchors()
        for a in node.ensure_connection_anchors():
            assert a.isVisible()
        node.hide_connection_anchors()
        for a in node.ensure_connection_anchors():
            assert not a.isVisible()


class TestConnectorItemSetters:
    def test_set_color_and_weight(self, qapp):
        c = ConnectorModel(source_id="a", target_id="b")
        item = ConnectorItem(c)
        item.set_color("#ff0000")
        item.set_weight(4.0)
        assert c.color == "#ff0000"
        assert c.weight == 4.0

    def test_set_line_style(self, qapp):
        c = ConnectorModel(source_id="a", target_id="b")
        item = ConnectorItem(c)
        item.set_line_style(LineStyle.DOTTED)
        assert c.line_style is LineStyle.DOTTED

    def test_set_endings(self, qapp):
        c = ConnectorModel(source_id="a", target_id="b")
        item = ConnectorItem(c)
        item.set_start_ending(EndingType.CIRCLE)
        item.set_end_ending(EndingType.CROSS)
        assert c.start_ending is EndingType.CIRCLE
        assert c.end_ending is EndingType.CROSS


class TestSceneConnectorCreation:
    def _make_node(self, scene: DiagramScene, node_id: str, x: float, y: float):
        node = NodeModel(
            id=node_id, shape_type=ShapeType.RECTANGLE, x=x, y=y, width=100, height=60
        )
        return scene.add_node(node)

    def test_set_connector_mode(self, qapp):
        scene = DiagramScene()
        scene.set_connector_mode(True)
        assert scene.is_connector_mode() is True
        scene.set_connector_mode(False)
        assert scene.is_connector_mode() is False

    def test_creation_lifecycle(self, qapp):
        scene = DiagramScene()
        self._make_node(scene, "a", 0, 0)
        self._make_node(scene, "b", 200, 0)

        # Start creation on a
        node_a = scene.get_node_item("a")
        scene._start_connector_creation(node_a, QPointF(50, 30))

        # Move to update preview
        scene.handle_mouse_move(QPointF(250, 30))
        assert scene._creation.active is True

        # Complete on node b
        node_b = scene.get_node_item("b")
        scene.handle_mouse_release(QPointF(250, 30), node_b)

        assert len(scene.document.connectors) == 1
        assert scene._creation.active is False

    def test_self_loop_rejected(self, qapp):
        scene = DiagramScene()
        self._make_node(scene, "a", 0, 0)
        node_a = scene.get_node_item("a")
        scene._start_connector_creation(node_a, QPointF(50, 30))
        scene.handle_mouse_move(QPointF(60, 30))
        scene.handle_mouse_release(QPointF(60, 30), node_a)
        assert len(scene.document.connectors) == 0

    def test_duplicate_allowed(self, qapp):
        scene = DiagramScene()
        self._make_node(scene, "a", 0, 0)
        self._make_node(scene, "b", 200, 0)
        node_a = scene.get_node_item("a")
        node_b = scene.get_node_item("b")

        # First A→B
        scene._start_connector_creation(node_a, QPointF(50, 30))
        scene.handle_mouse_move(QPointF(250, 30))
        scene.handle_mouse_release(QPointF(250, 30), node_b)

        # Second A→B (should be allowed per user decision)
        scene._start_connector_creation(node_a, QPointF(50, 30))
        scene.handle_mouse_move(QPointF(250, 30))
        scene.handle_mouse_release(QPointF(250, 30), node_b)

        assert len(scene.document.connectors) == 2

    def test_cancel_creation(self, qapp):
        scene = DiagramScene()
        self._make_node(scene, "a", 0, 0)
        node_a = scene.get_node_item("a")
        scene._start_connector_creation(node_a, QPointF(50, 30))
        scene.cancel_creation()
        assert scene._creation.active is False
        assert len(scene.document.connectors) == 0

    def test_set_connector_mode_off_cancels_creation(self, qapp):
        scene = DiagramScene()
        self._make_node(scene, "a", 0, 0)
        node_a = scene.get_node_item("a")
        scene._start_connector_creation(node_a, QPointF(50, 30))
        scene.set_connector_mode(False)
        assert scene._creation.active is False


class TestAnchorVisibilityOnSelection:
    def test_anchor_visible_when_node_selected(self, qapp):
        scene = DiagramScene()
        node = DiagramNodeItem(
            NodeModel(id="n1", shape_type=ShapeType.RECTANGLE, x=10, y=10)
        )
        scene.add_node(node.model)
        scene.get_node_item("n1").setSelected(True)

        # Trigger selectionChanged manually (Qt normally does this).
        scene._on_selection_changed()

        node_item = scene.get_node_item("n1")
        for anchor in node_item.ensure_connection_anchors():
            assert anchor.isVisible()

    def test_anchor_hidden_when_node_deselected(self, qapp):
        scene = DiagramScene()
        node = DiagramNodeItem(
            NodeModel(id="n1", shape_type=ShapeType.RECTANGLE, x=10, y=10)
        )
        scene.add_node(node.model)
        node_item = scene.get_node_item("n1")
        node_item.setSelected(True)
        scene._on_selection_changed()
        node_item.setSelected(False)
        scene._on_selection_changed()

        for anchor in node_item.ensure_connection_anchors():
            assert not anchor.isVisible()
