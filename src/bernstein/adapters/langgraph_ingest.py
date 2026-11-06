"""Ingest adapter for graph-shaped agent runtimes (LangGraph) (#4964).

Accepts execution output from graph-shaped agent runtimes (LangGraph),
validates declared nodes and edge references, digests tool-call
arguments, and maps the execution into Bernstein's lineage task graph
vocabulary (:class:`~bernstein.core.lineage.plan_render.RunPlan`).

The adapter is an out-of-core plugin conforming to the ingest contract
(#4963) via ``provide_ingest_adapter``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.observability.ingest_contract import (
    INGEST_EVENT_TYPES,
    IngestAdapterDeclaration,
)
from bernstein.plugins import hookimpl

if TYPE_CHECKING:
    from bernstein.core.lineage.plan_render import RunPlan


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON string without whitespace, sorted keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_args(args: Any) -> str:
    """Compute sha256 digest of tool call arguments."""
    if args is None:
        raw = "{}"
    elif isinstance(args, str):
        raw = args
    else:
        raw = _canonical_json(args)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _normalize_args(raw_args: Any) -> Any:
    """Normalize recorded arguments before computing or checking their digest."""
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            return {"raw": raw_args}
    return raw_args if isinstance(raw_args, (dict, list)) else {"raw": raw_args}


@dataclass(frozen=True, slots=True)
class LangGraphToolCall:
    """A tool call invoked during a graph node's execution."""

    name: str
    tool_call_id: str
    arguments: Any
    arguments_digest: str
    output: Any = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LangGraphToolCall:
        name = str(raw.get("name") or raw.get("tool") or "")
        tool_call_id = str(raw.get("id") or raw.get("tool_call_id") or "")
        args = _normalize_args(raw.get("args", raw.get("arguments", {})))

        computed_digest = _digest_args(args)
        given_digest = raw.get("arguments_digest")
        # The computed digest is always the canonical value; we only verify that
        # a caller-supplied digest (if present) matches it. If it does not match,
        # we raise. The recorded digest is always the computed one.
        if given_digest is not None and str(given_digest) != computed_digest:
            raise ValueError(
                f"Tool call arguments_digest mismatch for '{name}': "
                f"declared {given_digest} does not match computed {computed_digest}"
            )

        return cls(
            name=name,
            tool_call_id=tool_call_id,
            arguments=args,
            arguments_digest=computed_digest,
            output=raw.get("output"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "arguments": self.arguments,
            "arguments_digest": self.arguments_digest,
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class LangGraphNode:
    """One node in a LangGraph execution graph."""

    id: str
    name: str
    role: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    tool_calls: tuple[LangGraphToolCall, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LangGraphNode:
        node_id = str(raw.get("id") or raw.get("name") or "")
        name = str(raw.get("name") or node_id)
        role = str(raw.get("role") or "")
        inputs = dict(raw.get("inputs") or {}) if isinstance(raw.get("inputs"), dict) else {}
        outputs = dict(raw.get("outputs") or {}) if isinstance(raw.get("outputs"), dict) else {}
        raw_tools = raw.get("tool_calls", [])
        if raw_tools is None:
            raw_tools = []
        if not isinstance(raw_tools, list):
            raise ValueError("Node 'tool_calls' must be a list when present")
        tools = []
        for index, tool in enumerate(raw_tools):
            if not isinstance(tool, dict):
                raise ValueError(f"Tool call at index {index} must be a dictionary")
            tools.append(LangGraphToolCall.from_dict(tool))
        return cls(
            id=node_id,
            name=name,
            role=role,
            inputs=inputs,
            outputs=outputs,
            tool_calls=tuple(tools),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
        }


@dataclass(frozen=True, slots=True)
class LangGraphEdge:
    """A directed edge between nodes in a LangGraph graph."""

    source: str
    target: str
    conditional: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> LangGraphEdge:
        return cls(
            source=str(raw.get("source") or ""),
            target=str(raw.get("target") or ""),
            conditional=bool(raw.get("conditional", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "conditional": self.conditional,
        }


@dataclass(frozen=True, slots=True)
class IngestedGraphRun:
    """Ingested representation of a foreign LangGraph run."""

    run_id: str
    graph_id: str
    nodes: tuple[LangGraphNode, ...]
    edges: tuple[LangGraphEdge, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "metadata": self.metadata,
        }

    def canonical_bytes(self) -> bytes:
        """Deterministic UTF-8 canonical JSON serialization."""
        return _canonical_json(self.to_dict()).encode("utf-8")

    def to_lineage_run_plan(self, goal: str = "") -> RunPlan:
        """Map the foreign graph to Bernstein's RunPlan vocabulary."""
        from bernstein.core.lineage.plan_render import PlanNode, RunPlan

        incoming_deps: dict[str, set[str]] = {node.id: set() for node in self.nodes}
        for edge in self.edges:
            if edge.target in incoming_deps:
                incoming_deps[edge.target].add(edge.source)

        plan_nodes = tuple(
            PlanNode(
                task_id=node.id,
                role=node.role or "assistant",
                title=node.name,
                depends_on=tuple(sorted(incoming_deps.get(node.id, ()))),
            )
            for node in sorted(self.nodes, key=lambda n: n.id)
        )
        return RunPlan(
            goal=goal or f"LangGraph foreign run: {self.graph_id}",
            nodes=plan_nodes,
            recorded=True,
        )

    def render_graph_text(self, goal: str = "") -> str:
        """Render the plan as deterministic human-readable text."""
        from bernstein.core.lineage.plan_render import render_plan_text

        plan = self.to_lineage_run_plan(goal=goal)
        return render_plan_text(plan, run_id=self.run_id)


class LangGraphIngestAdapter:
    """Adapter translating LangGraph run traces into governed ingest events."""

    DECLARATION = IngestAdapterDeclaration(
        name="langgraph",
        version="1.0.0",
        declared_event_types=("gen_ai_activity", "untyped_activity"),
        summary="Ingest adapter for graph-shaped LangGraph runtimes.",
    )

    def validate_declaration(self, declaration: IngestAdapterDeclaration) -> None:
        """Validate that *declaration* conforms to the adapter's capabilities."""
        if not set(declaration.declared_event_types).issubset(INGEST_EVENT_TYPES):
            undeclared = set(declaration.declared_event_types) - set(INGEST_EVENT_TYPES)
            raise ValueError(f"Declaration contains unknown event types: {undeclared}")
        if not set(self.DECLARATION.declared_event_types).issubset(declaration.declared_event_types):
            missing = set(self.DECLARATION.declared_event_types) - set(declaration.declared_event_types)
            raise ValueError(f"Declaration does not declare required event types: {missing}")

    def ingest_trace(self, data: Mapping[str, Any] | str | Path) -> IngestedGraphRun:
        """Ingest a trace mapping, JSON string, or explicit Path to a UTF-8 file.

        Raises:
            ValueError: If a node present in the fixture is missing or malformed,
                preventing silent graph shrinkage.
        """
        raw_dict: dict[str, Any]
        if isinstance(data, Path):
            raw_dict = json.loads(data.read_text(encoding="utf-8"))
        elif isinstance(data, str):
            raw_dict = json.loads(data)
        elif isinstance(data, Mapping):
            raw_dict = dict(data)
        else:
            raise TypeError(f"Unsupported data type for LangGraph trace: {type(data)}")

        if not isinstance(raw_dict, dict):
            raise ValueError("LangGraph trace must be a JSON object")

        raw_meta = raw_dict.get("metadata")
        if raw_meta is not None and not isinstance(raw_meta, dict):
            raise ValueError("LangGraph trace 'metadata' must be a dictionary when present")

        run_id = str(raw_dict.get("run_id") or "foreign-langgraph-run")
        graph_id = str(raw_dict.get("graph_id") or "langgraph-graph")

        raw_nodes = raw_dict.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ValueError("LangGraph trace must contain a 'nodes' list")

        nodes: list[LangGraphNode] = []
        seen_node_ids: set[str] = set()
        for idx, item in enumerate(raw_nodes):
            if not isinstance(item, dict):
                raise ValueError(f"Node at index {idx} must be a dictionary")
            node = LangGraphNode.from_dict(item)
            if not node.id:
                raise ValueError(f"Node at index {idx} is missing an 'id' or 'name'")
            if node.id in seen_node_ids:
                raise ValueError(f"Duplicate node id '{node.id}' in graph nodes")
            seen_node_ids.add(node.id)
            nodes.append(node)

        raw_edges = raw_dict.get("edges", [])
        if raw_edges is None:
            raw_edges = []
        if not isinstance(raw_edges, list):
            raise ValueError("LangGraph trace 'edges' must be a list when present")

        edges: list[LangGraphEdge] = []
        for idx, e in enumerate(raw_edges):
            if not isinstance(e, dict):
                raise ValueError(f"Edge at index {idx} must be a dictionary")
            edge = LangGraphEdge.from_dict(e)
            if not edge.source or not edge.target:
                raise ValueError(f"Edge at index {idx} must have non-empty source and target")
            edges.append(edge)

        # Fail-closed validation: ensure all edges reference nodes present in the graph
        for edge in edges:
            if edge.source not in seen_node_ids:
                raise ValueError(f"Edge source '{edge.source}' not found in graph nodes: missing node reference")
            if edge.target not in seen_node_ids:
                raise ValueError(f"Edge target '{edge.target}' not found in graph nodes: missing node reference")

        nodes.sort(key=lambda n: n.id)
        edges.sort(key=lambda e: (e.source, e.target))

        metadata = dict(raw_meta or {})

        return IngestedGraphRun(
            run_id=run_id,
            graph_id=graph_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            metadata=metadata,
        )


class LangGraphCallbackHandler:
    """Hand-driven callback recorder producing an ingestable trace.

    Records the callback shapes tested here without third-party dependencies.
    Conformance to a real LangChain/LangGraph runtime is a separate integration.
    Payloads must be JSON-compatible and nested chain events must be balanced;
    parent-run routing, error callbacks and runtime-object serialization are not
    provided by this recorder.
    """

    def __init__(self, run_id: str, graph_id: str = "langgraph") -> None:
        self.run_id = run_id
        self.graph_id = graph_id
        self._nodes: list[dict[str, Any]] = []
        self._edges: list[dict[str, Any]] = []
        self._node_stack: list[dict[str, Any]] = []
        self._last_node_id: str | None = None

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any] | None,
        *,
        run_id: str | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        node_name = name or (serialized.get("name") if serialized else None) or "node"
        if node_name == self.graph_id:
            return

        occurrences = sum(1 for n in self._nodes if n.get("name") == node_name)
        node_id = run_id or (node_name if occurrences == 0 else f"{node_name}:{occurrences}")

        node_data = {
            "id": node_id,
            "name": node_name,
            "role": kwargs.get("role", "assistant"),
            "inputs": inputs or {},
            "outputs": {},
            "tool_calls": [],
        }
        self._nodes.append(node_data)
        if self._last_node_id and self._last_node_id != node_id:
            self._edges.append({"source": self._last_node_id, "target": node_id})
        self._node_stack.append(node_data)

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str | None = None,
        *,
        tool_call_id: str | None = None,
        name: str | None = None,
        args: Any = None,
        **kwargs: Any,
    ) -> None:
        if not self._node_stack:
            return
        current_node = self._node_stack[-1]
        tool_name = name or (serialized.get("name") if serialized else "tool")
        call_id = tool_call_id or f"call_{len(current_node['tool_calls']) + 1}"
        tool_args = _normalize_args(args if args is not None else {"input": input_str or ""})
        current_node["tool_calls"].append(
            {
                "name": tool_name,
                "id": call_id,
                "args": tool_args,
                "arguments_digest": _digest_args(tool_args),
            }
        )

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        if not self._node_stack:
            return
        tools = self._node_stack[-1]["tool_calls"]
        if tools:
            tools[-1]["output"] = output

    def on_chain_end(self, outputs: dict[str, Any] | None, **kwargs: Any) -> None:
        if self._node_stack:
            current_node = self._node_stack.pop()
            current_node["outputs"] = outputs or {}
            self._last_node_id = current_node["id"]

    def export_trace(self) -> dict[str, Any]:
        """Export accumulated run trace in LangGraph ingest format."""
        return {
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "nodes": list(self._nodes),
            "edges": list(self._edges),
        }


class LangGraphIngestPlugin:
    """Plugin registering the LangGraph ingest adapter into Bernstein."""

    @hookimpl
    def provide_ingest_adapter(self) -> IngestAdapterDeclaration:
        adapter = LangGraphIngestAdapter()
        adapter.validate_declaration(adapter.DECLARATION)
        return adapter.DECLARATION


__all__ = [
    "IngestedGraphRun",
    "LangGraphCallbackHandler",
    "LangGraphEdge",
    "LangGraphIngestAdapter",
    "LangGraphIngestPlugin",
    "LangGraphNode",
    "LangGraphToolCall",
]
