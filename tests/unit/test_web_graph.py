"""Tests for interactive browser-based task dependency graph (road-030)."""

from __future__ import annotations

from bernstein.core.web_graph import (
    GraphData,
    GraphEdge,
    GraphNode,
    build_graph_data,
    find_critical_path,
    render_graph_html,
)


def _task(
    tid: str,
    *,
    status: str = "open",
    role: str = "backend",
    priority: int = 2,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    """Create a minimal task dict for testing."""
    d: dict[str, object] = {
        "id": tid,
        "title": f"Task {tid}",
        "status": status,
        "role": role,
        "priority": priority,
    }
    if depends_on:
        d["depends_on"] = depends_on
    return d


# ---- build_graph_data ----


class TestBuildGraphData:
    def test_empty_input(self) -> None:
        result = build_graph_data([])
        assert result.nodes == []
        assert result.edges == []

    def test_single_task_no_deps(self) -> None:
        result = build_graph_data([_task("t1")])
        assert len(result.nodes) == 1
        assert result.nodes[0].id == "t1"
        assert result.nodes[0].status == "open"
        assert result.edges == []

    def test_two_tasks_with_dependency(self) -> None:
        tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
        result = build_graph_data(tasks)
        assert len(result.nodes) == 2
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.source == "t1"
        assert edge.target == "t2"
        assert edge.edge_type == "depends_on"

    def test_dangling_dependency_ignored(self) -> None:
        tasks = [_task("t1", depends_on=["missing"])]
        result = build_graph_data(tasks)
        assert len(result.nodes) == 1
        assert result.edges == []

    def test_multiple_deps(self) -> None:
        tasks = [_task("t1"), _task("t2"), _task("t3", depends_on=["t1", "t2"])]
        result = build_graph_data(tasks)
        assert len(result.edges) == 2
        sources = {e.source for e in result.edges}
        assert sources == {"t1", "t2"}

    def test_fields_mapped(self) -> None:
        tasks = [_task("x", status="done", role="qa", priority=1)]
        result = build_graph_data(tasks)
        n = result.nodes[0]
        assert n.status == "done"
        assert n.role == "qa"
        assert n.priority == 1


# ---- render_graph_html ----


class TestRenderGraphHtml:
    def test_contains_d3_reference(self) -> None:
        page = render_graph_html(GraphData())
        assert "d3.v7.min.js" in page

    def test_contains_svg(self) -> None:
        page = render_graph_html(GraphData())
        assert "<svg" in page

    def test_contains_script(self) -> None:
        page = render_graph_html(GraphData())
        assert "<script" in page

    def test_contains_html_doctype(self) -> None:
        page = render_graph_html(GraphData())
        assert "<!DOCTYPE html>" in page

    def test_nodes_embedded(self) -> None:
        data = build_graph_data([_task("demo", status="done")])
        page = render_graph_html(data)
        assert "demo" in page
        assert "Task demo" in page

    def test_filter_controls_present(self) -> None:
        data = build_graph_data([_task("t1", role="qa")])
        page = render_graph_html(data)
        assert "status-filter" in page
        assert "role-filter" in page
        assert "qa" in page

    def test_inspector_panel_present(self) -> None:
        page = render_graph_html(GraphData())
        assert "inspector" in page

    def test_zoom_pan_enabled(self) -> None:
        page = render_graph_html(GraphData())
        assert "d3.zoom" in page


# ---- find_critical_path ----


class TestFindCriticalPath:
    def test_empty_graph(self) -> None:
        result = find_critical_path(GraphData())
        assert result == []

    def test_no_edges(self) -> None:
        data = GraphData(
            nodes=[GraphNode("a", "A", "open", "be", 2), GraphNode("b", "B", "open", "be", 2)],
            edges=[],
        )
        result = find_critical_path(data)
        # With no edges, path is a single node
        assert len(result) == 1

    def test_linear_chain(self) -> None:
        nodes = [
            GraphNode("a", "A", "open", "be", 2),
            GraphNode("b", "B", "open", "be", 2),
            GraphNode("c", "C", "open", "be", 2),
        ]
        edges = [
            GraphEdge("a", "b", "depends_on"),
            GraphEdge("b", "c", "depends_on"),
        ]
        result = find_critical_path(GraphData(nodes=nodes, edges=edges))
        assert result == ["a", "b", "c"]

    def test_diamond_graph(self) -> None:
        """Diamond: a -> b, a -> c, b -> d, c -> d.

        Both paths a-b-d and a-c-d have length 3 (equal).
        The critical path should be one of them.
        """
        nodes = [
            GraphNode("a", "A", "open", "be", 2),
            GraphNode("b", "B", "open", "be", 2),
            GraphNode("c", "C", "open", "be", 2),
            GraphNode("d", "D", "open", "be", 2),
        ]
        edges = [
            GraphEdge("a", "b", "depends_on"),
            GraphEdge("a", "c", "depends_on"),
            GraphEdge("b", "d", "depends_on"),
            GraphEdge("c", "d", "depends_on"),
        ]
        result = find_critical_path(GraphData(nodes=nodes, edges=edges))
        assert len(result) == 3
        assert result[0] == "a"
        assert result[-1] == "d"
        assert result[1] in ("b", "c")

    def test_longer_branch_wins(self) -> None:
        """Two branches from root: short (a->d) and long (a->b->c->d).

        The critical path should follow the long branch.
        """
        nodes = [
            GraphNode("a", "A", "open", "be", 2),
            GraphNode("b", "B", "open", "be", 2),
            GraphNode("c", "C", "open", "be", 2),
            GraphNode("d", "D", "open", "be", 2),
        ]
        edges = [
            GraphEdge("a", "b", "depends_on"),
            GraphEdge("b", "c", "depends_on"),
            GraphEdge("c", "d", "depends_on"),
            GraphEdge("a", "d", "depends_on"),
        ]
        result = find_critical_path(GraphData(nodes=nodes, edges=edges))
        assert result == ["a", "b", "c", "d"]

    def test_single_node(self) -> None:
        data = GraphData(nodes=[GraphNode("only", "Only", "open", "be", 2)])
        result = find_critical_path(data)
        assert result == ["only"]

    def test_build_then_critical_path(self) -> None:
        """End-to-end: build_graph_data then find_critical_path."""
        tasks = [
            _task("t1"),
            _task("t2", depends_on=["t1"]),
            _task("t3", depends_on=["t2"]),
        ]
        data = build_graph_data(tasks)
        result = find_critical_path(data)
        assert result == ["t1", "t2", "t3"]
