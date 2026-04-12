"""Interactive browser-based task dependency graph visualisation.

Builds a ``GraphData`` model from raw task dicts, renders a self-contained
HTML page with an inline D3.js force-directed layout, and computes the
critical path (longest dependency chain) through the DAG.

The HTML output supports:

* Force-directed layout with draggable nodes
* Colour-coded nodes by task status
* Click-to-inspect panel showing task details
* Zoom and pan via mouse / trackpad
* Filter controls for status and role
"""

from __future__ import annotations

import html
import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphNode:
    """A single node in the dependency graph.

    Attributes:
        id: Unique task identifier.
        title: Human-readable task title.
        status: Current task status string (e.g. ``"open"``, ``"done"``).
        role: Agent role assigned to the task.
        priority: Numeric priority (1 = critical, 3 = nice-to-have).
        x: Optional initial x coordinate for layout.
        y: Optional initial y coordinate for layout.
    """

    id: str
    title: str
    status: str
    role: str
    priority: int
    x: float | None = None
    y: float | None = None


@dataclass(frozen=True)
class GraphEdge:
    """A directed edge between two nodes.

    Attributes:
        source: ID of the upstream (dependency) node.
        target: ID of the downstream (dependent) node.
        edge_type: Semantic relationship type.
    """

    source: str
    target: str
    edge_type: Literal["depends_on", "blocks"]


@dataclass(frozen=True)
class GraphData:
    """Complete graph payload for rendering.

    Attributes:
        nodes: All nodes in the graph.
        edges: All directed edges.
    """

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph_data(tasks: list[dict[str, object]]) -> GraphData:
    """Extract nodes and edges from raw task dictionaries.

    Each task dict is expected to have ``id``, ``title``, ``status``,
    ``role``, ``priority``, and optionally ``depends_on`` (list of task
    IDs).  Missing optional fields receive sensible defaults.

    Args:
        tasks: List of task dicts as returned by the task store API.

    Returns:
        Populated ``GraphData`` ready for rendering or analysis.
    """
    task_ids: set[str] = {str(t["id"]) for t in tasks}

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    for t in tasks:
        tid = str(t["id"])
        nodes.append(
            GraphNode(
                id=tid,
                title=str(t.get("title", tid)),
                status=str(t.get("status", "open")),
                role=str(t.get("role", "backend")),
                priority=int(t.get("priority", 2)),  # type: ignore[arg-type]
            )
        )
        deps: list[str] = list(t.get("depends_on", []))  # type: ignore[arg-type]
        for dep in deps:
            dep_str = str(dep)
            if dep_str in task_ids:
                edges.append(GraphEdge(source=dep_str, target=tid, edge_type="depends_on"))

    return GraphData(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------


def find_critical_path(data: GraphData) -> list[str]:
    """Find the longest dependency chain in the graph.

    Uses topological-order dynamic programming.  If the graph has no
    edges the result is either a single-node list (if nodes exist) or
    empty.  Ties are broken by insertion order.

    Args:
        data: The graph to analyse.

    Returns:
        List of task IDs forming the longest chain, from root to leaf.
    """
    if not data.nodes:
        return []

    node_ids: set[str] = {n.id for n in data.nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}

    for edge in data.edges:
        if edge.source in node_ids and edge.target in node_ids:
            adjacency[edge.source].append(edge.target)
            in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

    # Kahn's algorithm for topological order
    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    topo_order: list[str] = []
    while queue:
        node = queue.popleft()
        topo_order.append(node)
        for successor in adjacency[node]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if not topo_order:
        return [data.nodes[0].id]

    # DP for longest path
    dist: dict[str, int] = {nid: 1 for nid in node_ids}
    predecessor: dict[str, str | None] = {nid: None for nid in node_ids}

    for node in topo_order:
        for successor in adjacency[node]:
            if dist[node] + 1 > dist[successor]:
                dist[successor] = dist[node] + 1
                predecessor[successor] = node

    # Trace back from the node with maximum distance
    end_node = max(topo_order, key=lambda nid: dist[nid])
    path: list[str] = []
    current: str | None = end_node
    while current is not None:
        path.append(current)
        current = predecessor[current]
    path.reverse()
    return path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STATUS_COLOURS: dict[str, str] = {
    "open": "#3b82f6",
    "claimed": "#f59e0b",
    "in_progress": "#8b5cf6",
    "done": "#10b981",
    "closed": "#6b7280",
    "failed": "#ef4444",
    "blocked": "#f97316",
    "cancelled": "#9ca3af",
    "planned": "#06b6d4",
    "orphaned": "#dc2626",
}

_DEFAULT_COLOUR = "#64748b"


def render_graph_html(data: GraphData) -> str:
    """Generate a self-contained HTML page with an interactive D3.js graph.

    The page includes:

    * Force-directed layout with collision avoidance
    * Colour-coded nodes by task status
    * Arrows on edges indicating dependency direction
    * Click-to-inspect sidebar showing task details
    * Zoom and pan via mouse wheel / drag
    * Filter dropdowns for status and role

    Args:
        data: The graph data to render.

    Returns:
        Complete HTML document as a string.
    """
    nodes_json = json.dumps(
        [
            {
                "id": n.id,
                "title": html.escape(n.title),
                "status": n.status,
                "role": n.role,
                "priority": n.priority,
                "color": _STATUS_COLOURS.get(n.status, _DEFAULT_COLOUR),
            }
            for n in data.nodes
        ]
    )
    edges_json = json.dumps([{"source": e.source, "target": e.target, "edge_type": e.edge_type} for e in data.edges])
    statuses = sorted({n.status for n in data.nodes})
    roles = sorted({n.role for n in data.nodes})
    status_options = "".join(f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in statuses)
    role_options = "".join(f'<option value="{html.escape(r)}">{html.escape(r)}</option>' for r in roles)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bernstein — Task Dependency Graph</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; display: flex; height: 100vh; }}
  #controls {{ width: 220px; padding: 16px; background: #1e293b; overflow-y: auto;
               border-right: 1px solid #334155; flex-shrink: 0; }}
  #controls h2 {{ font-size: 14px; margin-bottom: 12px; color: #94a3b8; text-transform: uppercase; }}
  #controls label {{ display: block; font-size: 12px; color: #94a3b8; margin-top: 10px; }}
  #controls select {{ width: 100%; padding: 6px; margin-top: 4px; background: #0f172a;
                      color: #e2e8f0; border: 1px solid #475569; border-radius: 4px; }}
  #graph-container {{ flex: 1; position: relative; }}
  svg {{ width: 100%; height: 100%; }}
  .node circle {{ cursor: pointer; stroke: #1e293b; stroke-width: 2; }}
  .node text {{ font-size: 11px; fill: #cbd5e1; pointer-events: none; }}
  .link {{ stroke: #475569; stroke-width: 1.5; fill: none; marker-end: url(#arrow); }}
  #inspector {{ position: absolute; top: 16px; right: 16px; width: 260px; background: #1e293b;
                border: 1px solid #334155; border-radius: 8px; padding: 16px;
                display: none; font-size: 13px; }}
  #inspector h3 {{ font-size: 15px; margin-bottom: 8px; }}
  #inspector .field {{ margin-top: 6px; }}
  #inspector .label {{ color: #94a3b8; font-size: 11px; text-transform: uppercase; }}
  #inspector .value {{ margin-top: 2px; }}
  .legend {{ margin-top: 20px; }}
  .legend-item {{ display: flex; align-items: center; margin-top: 6px; font-size: 12px; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; flex-shrink: 0; }}
</style>
</head>
<body>
<div id="controls">
  <h2>Filters</h2>
  <label for="status-filter">Status</label>
  <select id="status-filter"><option value="all">All</option>{status_options}</select>
  <label for="role-filter">Role</label>
  <select id="role-filter"><option value="all">All</option>{role_options}</select>
  <div class="legend">
    <h2>Legend</h2>
    <div class="legend-item"><span class="legend-dot" style="background:#3b82f6"></span>open</div>
    <div class="legend-item"><span class="legend-dot" style="background:#f59e0b"></span>claimed</div>
    <div class="legend-item"><span class="legend-dot" style="background:#8b5cf6"></span>in_progress</div>
    <div class="legend-item"><span class="legend-dot" style="background:#10b981"></span>done</div>
    <div class="legend-item"><span class="legend-dot" style="background:#ef4444"></span>failed</div>
    <div class="legend-item"><span class="legend-dot" style="background:#f97316"></span>blocked</div>
    <div class="legend-item"><span class="legend-dot" style="background:#6b7280"></span>closed</div>
  </div>
</div>
<div id="graph-container">
  <svg id="graph-svg">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="22" refY="5"
              markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#475569"/>
      </marker>
    </defs>
  </svg>
  <div id="inspector">
    <h3 id="insp-title"></h3>
    <div class="field"><div class="label">ID</div><div class="value" id="insp-id"></div></div>
    <div class="field"><div class="label">Status</div><div class="value" id="insp-status"></div></div>
    <div class="field"><div class="label">Role</div><div class="value" id="insp-role"></div></div>
    <div class="field"><div class="label">Priority</div><div class="value" id="insp-priority"></div></div>
  </div>
</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const rawNodes = {nodes_json};
const rawEdges = {edges_json};
let nodes = rawNodes.map(d => ({{...d}}));
let edges = rawEdges.map(d => ({{...d}}));

const svg = d3.select("#graph-svg");
const container = svg.append("g");

// Zoom & pan
svg.call(d3.zoom().scaleExtent([0.1, 4]).on("zoom", (e) => {{
  container.attr("transform", e.transform);
}}));

const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(d => d.id).distance(120))
  .force("charge", d3.forceManyBody().strength(-300))
  .force("center", d3.forceCenter(
    document.getElementById("graph-container").clientWidth / 2,
    document.getElementById("graph-container").clientHeight / 2))
  .force("collision", d3.forceCollide().radius(30));

const link = container.append("g").selectAll("line")
  .data(edges).enter().append("line").attr("class", "link");

const node = container.append("g").selectAll("g")
  .data(nodes).enter().append("g").attr("class", "node");

node.append("circle").attr("r", 12).attr("fill", d => d.color)
  .on("click", (event, d) => {{
    const insp = document.getElementById("inspector");
    insp.style.display = "block";
    document.getElementById("insp-title").textContent = d.title;
    document.getElementById("insp-id").textContent = d.id;
    document.getElementById("insp-status").textContent = d.status;
    document.getElementById("insp-role").textContent = d.role;
    document.getElementById("insp-priority").textContent = d.priority;
  }});

node.append("text").text(d => d.title.length > 20 ? d.title.slice(0, 18) + "..." : d.title)
  .attr("dx", 16).attr("dy", 4);

// Drag
node.call(d3.drag()
  .on("start", (e, d) => {{ if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
  .on("drag", (e, d) => {{ d.fx = e.x; d.fy = e.y; }})
  .on("end", (e, d) => {{ if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }}));

simulation.on("tick", () => {{
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
}});

// Filters
function applyFilters() {{
  const sf = document.getElementById("status-filter").value;
  const rf = document.getElementById("role-filter").value;
  node.style("display", d => {{
    if (sf !== "all" && d.status !== sf) return "none";
    if (rf !== "all" && d.role !== rf) return "none";
    return null;
  }});
  const visible = new Set();
  node.each(function(d) {{ if (d3.select(this).style("display") !== "none") visible.add(d.id); }});
  link.style("display", d => {{
    const sid = typeof d.source === "object" ? d.source.id : d.source;
    const tid = typeof d.target === "object" ? d.target.id : d.target;
    return visible.has(sid) && visible.has(tid) ? null : "none";
  }});
}}
document.getElementById("status-filter").addEventListener("change", applyFilters);
document.getElementById("role-filter").addEventListener("change", applyFilters);
</script>
</body>
</html>"""
