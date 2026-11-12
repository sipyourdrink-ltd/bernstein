"""Tests for graph trace ingest adapter (issue #4964).

Proof assertions from #4964:
- a synthetic fixture ingests to a graph whose nodes and edges match the source;
- every tool call in the fixture appears with a digest of its arguments;
- the rendered graph is byte-identical across two ingests of the same fixture;
- a node present in the fixture but absent from the ingest fails the test rather
  than silently shrinking the graph.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from bernstein.adapters.graph_trace_ingest import (
    GraphTraceCallbackHandler,
    GraphTraceIngestAdapter,
    GraphTraceIngestPlugin,
)
from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration
from bernstein.core.trackers import registry
from bernstein.plugins.manager import PluginManager

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "govern" / "graph_trace_run.json"


@pytest.fixture(autouse=True)
def isolated_ingest_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_ingest_declarations", {})


@pytest.fixture
def adapter() -> GraphTraceIngestAdapter:
    return GraphTraceIngestAdapter()


@pytest.fixture
def raw_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_graph_trace_fixture_ingests_to_matching_graph(
    adapter: GraphTraceIngestAdapter,
    raw_fixture: dict,
) -> None:
    """Nodes and edges from the source fixture are faithfully preserved."""
    run = adapter.ingest_trace(FIXTURE_PATH)

    assert run.run_id == raw_fixture["run_id"]
    assert run.graph_id == raw_fixture["graph_id"]

    source_node_ids = {n["id"] for n in raw_fixture["nodes"]}
    ingested_node_ids = {n.id for n in run.nodes}
    assert ingested_node_ids == source_node_ids

    source_edges = {(e["source"], e["target"]) for e in raw_fixture["edges"]}
    ingested_edges = {(e.source, e.target) for e in run.edges}
    assert ingested_edges == source_edges

    expected = copy.deepcopy(raw_fixture)
    expected["nodes"].sort(key=lambda node: node["id"])
    expected["edges"].sort(key=lambda edge: (edge["source"], edge["target"]))
    for node in expected["nodes"]:
        for tool in node["tool_calls"]:
            tool["tool_call_id"] = tool.pop("id")
            tool["arguments"] = tool.pop("args")
            canonical = json.dumps(tool["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            tool["arguments_digest"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert run.to_dict() == expected


def test_every_tool_call_has_arguments_digest(
    adapter: GraphTraceIngestAdapter,
) -> None:
    """Every tool call in the fixture appears with a sha256 digest of its arguments."""
    run = adapter.ingest_trace(FIXTURE_PATH)

    tool_calls = [tc for node in run.nodes for tc in node.tool_calls]
    assert len(tool_calls) == 2

    for tc in tool_calls:
        assert tc.arguments_digest.startswith("sha256:")
        assert len(tc.arguments_digest) == 71  # "sha256:" (7) + 64 hex chars
        assert tc.name
        assert tc.tool_call_id


def test_rendered_graph_is_byte_identical_across_two_ingests(
    adapter: GraphTraceIngestAdapter,
) -> None:
    """Determinism property: two ingests of the same fixture produce identical bytes."""
    run_1 = adapter.ingest_trace(FIXTURE_PATH)
    run_2 = adapter.ingest_trace(FIXTURE_PATH)

    assert run_1.canonical_bytes() == run_2.canonical_bytes()

    text_1 = run_1.render_graph_text()
    text_2 = run_2.render_graph_text()
    assert text_1 == text_2
    assert "QueryRouterNode" in text_1
    assert "PolicyRetrieverNode" in text_1
    assert "ResponseSynthesizerNode" in text_1


def test_node_missing_from_ingest_fails_rather_than_silently_shrinking(
    adapter: GraphTraceIngestAdapter,
    raw_fixture: dict,
) -> None:
    """A node present in source but missing/dropped in ingest fails validation."""
    tampered = copy.deepcopy(raw_fixture)
    # Remove the retriever node while edges still reference it
    tampered["nodes"] = [n for n in tampered["nodes"] if n["id"] != "retriever"]

    with pytest.raises(ValueError, match="not found in graph nodes: missing node reference"):
        adapter.ingest_trace(tampered)

    # If raw fixture had an invalid entry without an id
    malformed = copy.deepcopy(raw_fixture)
    malformed["nodes"].append({"invalid": "missing id"})
    with pytest.raises(ValueError, match="missing an 'id' or 'name'"):
        adapter.ingest_trace(malformed)


def test_tool_call_with_list_arguments(
    adapter: GraphTraceIngestAdapter,
) -> None:
    """Tool calls with list arguments (raw or JSON string) parse and digest without crash."""
    from bernstein.adapters.graph_trace_ingest import GraphTraceToolCall

    tc_list = GraphTraceToolCall.from_dict(
        {
            "name": "batch_search",
            "id": "call_1",
            "args": [1, 2, 3],
        }
    )
    assert tc_list.arguments == [1, 2, 3]
    assert tc_list.arguments_digest.startswith("sha256:")

    tc_json_list = GraphTraceToolCall.from_dict(
        {
            "name": "batch_search",
            "id": "call_2",
            "args": '["a", "b", "c"]',
        }
    )
    assert tc_json_list.arguments == ["a", "b", "c"]
    assert tc_json_list.arguments_digest.startswith("sha256:")


def test_callback_handler_preserves_graph_cycles() -> None:
    """Nodes executing multiple times in loops/cycles are preserved without overwriting."""
    handler = GraphTraceCallbackHandler(run_id="run-cycle", graph_id="cycle-graph")

    # Step 1: agent runs
    handler.on_chain_start(serialized={"name": "agent"}, inputs={"msg": "start"}, name="agent")
    handler.on_chain_end(outputs={"msg": "call tool"})

    # Step 2: tools runs
    handler.on_chain_start(serialized={"name": "tools"}, inputs={"msg": "execute"}, name="tools")
    handler.on_chain_end(outputs={"msg": "tool done"})

    # Step 3: agent runs AGAIN (cycle)
    handler.on_chain_start(serialized={"name": "agent"}, inputs={"msg": "evaluate"}, name="agent")
    handler.on_chain_end(outputs={"msg": "final answer"})

    trace = handler.export_trace()
    assert len(trace["nodes"]) == 3
    node_ids = [n["id"] for n in trace["nodes"]]
    assert node_ids == ["agent", "tools", "agent:1"]

    edges = [(e["source"], e["target"]) for e in trace["edges"]]
    assert ("agent", "tools") in edges
    assert ("tools", "agent:1") in edges


def test_lineage_run_plan_mapping(
    adapter: GraphTraceIngestAdapter,
) -> None:
    """Foreign graph maps directly to Bernstein's RunPlan and PlanNode vocabulary."""
    run = adapter.ingest_trace(FIXTURE_PATH)
    plan = run.to_lineage_run_plan()

    assert plan.recorded is True
    assert len(plan.nodes) == 3

    node_by_id = {n.task_id: n for n in plan.nodes}
    assert node_by_id["router"].depends_on == ()
    assert node_by_id["retriever"].depends_on == ("router",)
    assert node_by_id["synthesizer"].depends_on == ("retriever",)


def test_graph_trace_callback_handler_records_trace(
    adapter: GraphTraceIngestAdapter,
) -> None:
    """GraphTraceCallbackHandler intercepts execution callbacks into an ingestable trace."""
    handler = GraphTraceCallbackHandler(run_id="run-cb-123", graph_id="agent-graph")

    # Step 1: node 1
    handler.on_chain_start(
        serialized={"name": "planner"},
        inputs={"task": "do research"},
        name="planner",
        role="planner",
    )
    handler.on_tool_start(
        serialized={"name": "web_search"},
        tool_call_id="call_99",
        name="web_search",
        args={"query": "test query"},
    )
    handler.on_tool_end(output={"results": ["doc1"]})
    handler.on_chain_end(outputs={"plan": "completed"})

    # Step 2: node 2
    handler.on_chain_start(
        serialized={"name": "writer"},
        inputs={"data": "doc1"},
        name="writer",
        role="writer",
    )
    handler.on_chain_end(outputs={"doc": "final text"})

    trace = handler.export_trace()
    assert trace["run_id"] == "run-cb-123"
    assert len(trace["nodes"]) == 2
    assert len(trace["edges"]) == 1
    assert trace["edges"][0] == {"source": "planner", "target": "writer"}

    # Must ingest cleanly through the adapter
    run = adapter.ingest_trace(trace)
    assert len(run.nodes) == 2
    assert len(run.edges) == 1
    assert run.nodes[0].tool_calls[0].arguments_digest.startswith("sha256:")


def test_plugin_contract_integration(tmp_path: Path) -> None:
    """Plugin conforms to provide_ingest_adapter hook and registers cleanly."""
    plugin = GraphTraceIngestPlugin()
    decl = plugin.provide_ingest_adapter()

    assert isinstance(decl, IngestAdapterDeclaration)
    assert decl.name == "graph_trace_ingest"
    assert "gen_ai_activity" in decl.declared_event_types
    assert "untyped_activity" in decl.declared_event_types

    pm = PluginManager(workdir=tmp_path)
    pm.register(plugin, name="graph-trace-ingest-plugin")
    added = pm.collect_plugin_ingest_adapters()
    assert added == 1
    assert registry._ingest_declarations["graph_trace_ingest"] == decl
    assert pm.collect_plugin_ingest_adapters() == 0


def test_tool_call_arguments_digest_mismatch_raises(adapter: GraphTraceIngestAdapter) -> None:
    """Declared arguments_digest that does not match computed digest raises ValueError."""
    from bernstein.adapters.graph_trace_ingest import GraphTraceToolCall

    with pytest.raises(ValueError, match="arguments_digest mismatch"):
        GraphTraceToolCall.from_dict(
            {
                "name": "search",
                "args": {"q": "test"},
                "arguments_digest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            }
        )


def test_duplicate_node_id_fails(adapter: GraphTraceIngestAdapter, raw_fixture: dict) -> None:
    """Duplicate node ids in graph nodes raise ValueError."""
    tampered = copy.deepcopy(raw_fixture)
    tampered["nodes"].append(copy.deepcopy(tampered["nodes"][0]))
    with pytest.raises(ValueError, match="Duplicate node id"):
        adapter.ingest_trace(tampered)


def test_malformed_edge_missing_target_fails(adapter: GraphTraceIngestAdapter, raw_fixture: dict) -> None:
    """Edges missing source or target raise ValueError."""
    tampered = copy.deepcopy(raw_fixture)
    tampered["edges"].append({"source": "router", "target": ""})
    with pytest.raises(ValueError, match="target must be a non-empty string"):
        adapter.ingest_trace(tampered)


def test_ingest_trace_from_str_path(adapter: GraphTraceIngestAdapter) -> None:
    """Only an explicit Path permits reading local files; strings are JSON."""
    with pytest.raises(json.JSONDecodeError):
        adapter.ingest_trace(str(FIXTURE_PATH))


@pytest.mark.parametrize(
    "arguments",
    [{"query": "café"}, [1, 2], "plain text", '{"query":"café"}', "[1,2]", "42", 42, 0, False, ""],
)
def test_callback_arguments_round_trip_with_a_digest_of_the_recorded_value(arguments: object) -> None:
    handler = GraphTraceCallbackHandler(run_id="round-trip")
    handler.on_chain_start({"name": "agent"}, {})
    handler.on_tool_start({"name": "search"}, args=arguments)
    handler.on_chain_end({"done": True})

    run = GraphTraceIngestAdapter().ingest_trace(handler.export_trace())
    tool = run.nodes[0].tool_calls[0]
    canonical = json.dumps(tool.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert tool.arguments_digest == "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert (
        GraphTraceIngestAdapter().ingest_trace(json.loads(run.canonical_bytes())).canonical_bytes()
        == run.canonical_bytes()
    )


def test_nested_callbacks_preserve_outer_outputs_and_tools_after_the_inner_chain() -> None:
    handler = GraphTraceCallbackHandler(run_id="nested")
    handler.on_chain_start({"name": "outer"}, {}, run_id="outer")
    handler.on_tool_start({"name": "before"}, args={"step": 1})
    handler.on_tool_end({"result": "before"})
    handler.on_chain_start({"name": "inner"}, {}, run_id="inner")
    handler.on_tool_start({"name": "inside"}, args={"step": 2})
    handler.on_tool_end({"result": "inside"})
    handler.on_chain_end({"result": "inner"})
    handler.on_tool_start({"name": "after"}, args={"step": 3})
    handler.on_tool_end({"result": "after"})
    handler.on_chain_end({"result": "outer"})

    nodes = {node.id: node for node in GraphTraceIngestAdapter().ingest_trace(handler.export_trace()).nodes}
    assert nodes["outer"].outputs == {"result": "outer"}
    assert [tool.name for tool in nodes["outer"].tool_calls] == ["before", "after"]
    assert nodes["outer"].tool_calls[-1].output == {"result": "after"}
    assert [tool.name for tool in nodes["inner"].tool_calls] == ["inside"]
    assert nodes["inner"].outputs == {"result": "inner"}


@pytest.mark.parametrize("tool_calls", [["unparsed"], [None], [{"name": "valid"}, 42], "unparsed", {}])
def test_malformed_tool_calls_fail_explicitly_instead_of_disappearing(tool_calls: object) -> None:
    with pytest.raises(ValueError, match="tool_calls|Tool call"):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [{"id": "agent", "tool_calls": tool_calls}]})


def test_json_trace_strings_never_probe_the_local_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    def prohibited(*args: object, **kwargs: object) -> bool:
        pytest.fail("A JSON trace string must not be inspected as a local path")

    monkeypatch.setattr(Path, "is_file", prohibited)
    assert GraphTraceIngestAdapter().ingest_trace('{"nodes": []}').nodes == ()


@pytest.mark.parametrize("arguments", [0, False, "", None, []])
def test_falsey_arguments_are_preserved_instead_of_replaced_by_an_empty_object(arguments: object) -> None:
    from bernstein.adapters.graph_trace_ingest import GraphTraceToolCall

    tool = GraphTraceToolCall.from_dict({"name": "tool", "args": arguments})
    assert tool.arguments == (arguments if isinstance(arguments, list) else {"raw": arguments})


@pytest.mark.parametrize("edges", [{}, "", False])
def test_malformed_edge_collections_do_not_disappear_when_falsey(edges: object) -> None:
    with pytest.raises(ValueError, match="edges.*list"):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [], "edges": edges})


@pytest.mark.parametrize("payload", ["[]", "null", "42"])
def test_json_trace_requires_an_object_with_a_graph(payload: str) -> None:
    with pytest.raises(ValueError, match="object"):
        GraphTraceIngestAdapter().ingest_trace(payload)


def test_plugin_rejects_an_unknown_event_declaration_before_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    invalid = IngestAdapterDeclaration("graph_trace_ingest", "1.0.0", ("unknown_activity",))
    monkeypatch.setattr(GraphTraceIngestAdapter, "DECLARATION", invalid)
    with pytest.raises(ValueError, match="unknown event types"):
        GraphTraceIngestPlugin().provide_ingest_adapter()


@pytest.mark.parametrize("field", ["inputs", "outputs"])
@pytest.mark.parametrize("value", ["state", [1], None, False, 0])
def test_node_state_must_be_a_mapping(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [{"id": "agent", field: value}]})


def test_node_state_preserves_mappings() -> None:
    state = MappingProxyType({"messages": ["hello"]})
    run = GraphTraceIngestAdapter().ingest_trace({"nodes": [{"id": "agent", "inputs": state, "outputs": state}]})
    assert run.nodes[0].inputs == run.nodes[0].outputs == state
    assert run.nodes[0].inputs is not state


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, {}, []])
def test_edge_conditional_must_be_a_boolean(value: object) -> None:
    with pytest.raises(ValueError, match="conditional"):
        GraphTraceIngestAdapter().ingest_trace(
            {"nodes": [{"id": "agent"}], "edges": [{"source": "agent", "target": "agent", "conditional": value}]}
        )


@pytest.mark.parametrize("conditional", [False, True])
def test_edge_conditional_and_cycles_are_preserved(conditional: bool) -> None:
    run = GraphTraceIngestAdapter().ingest_trace(
        {"nodes": [{"id": "agent"}], "edges": [{"source": "agent", "target": "agent", "conditional": conditional}]}
    )
    assert run.edges[0].conditional is conditional
    assert run.to_lineage_run_plan().nodes[0].depends_on == ("agent",)


@pytest.mark.parametrize("field", ["id", "name", "role"])
@pytest.mark.parametrize("value", [None, 0, False, {}, []])
def test_node_text_fields_are_not_coerced(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [{"id": "agent", "name": "Agent", field: value}]})


def test_explicit_empty_node_id_does_not_fall_back_to_name() -> None:
    with pytest.raises(ValueError, match="id"):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [{"id": "", "name": "agent"}]})


@pytest.mark.parametrize("field", ["source", "target"])
@pytest.mark.parametrize("value", ["", None, 0, False, {}, []])
def test_edge_endpoints_must_be_nonempty_strings(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        GraphTraceIngestAdapter().ingest_trace(
            {"nodes": [{"id": "agent"}], "edges": [{"source": "agent", "target": "agent", field: value}]}
        )


@pytest.mark.parametrize("tool", [{}, {"id": "call"}, {"name": ""}, {"name": None}, {"name": 42}, {"tool": {}}])
def test_tool_call_requires_a_nonempty_string_name(tool: dict) -> None:
    with pytest.raises(ValueError, match="Tool call.*name"):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [{"id": "agent", "tool_calls": [tool]}]})


@pytest.mark.parametrize("field", ["id", "tool_call_id"])
@pytest.mark.parametrize("value", [None, 42, {}, []])
def test_supplied_tool_call_ids_are_not_coerced(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="Tool call.*id"):
        GraphTraceIngestAdapter().ingest_trace(
            {"nodes": [{"id": "agent", "tool_calls": [{"name": "tool", field: value}]}]}
        )


@pytest.mark.parametrize("field", ["run_id", "graph_id", "id", "name", "role", "source", "target"])
@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"])
def test_foreign_render_fields_reject_line_and_control_injection(field: str, control: str) -> None:
    trace = {
        "nodes": [{"id": "agent", "name": "Agent", "role": "assistant"}],
        "edges": [{"source": "agent", "target": "agent"}],
    }
    value = f"agent{control}run: forged{control}  forged  [admin]  injected task"
    if field in {"run_id", "graph_id"}:
        trace[field] = value
    elif field in {"source", "target"}:
        trace["edges"][0][field] = value
    else:
        trace["nodes"][0][field] = value

    rendered = []
    with pytest.raises(ValueError, match="control characters"):
        rendered.append(GraphTraceIngestAdapter().ingest_trace(trace).render_graph_text())
    assert rendered == []  # Rejection occurs before any forged header or task can be rendered.


@pytest.mark.parametrize("field", ["run_id", "graph_id"])
@pytest.mark.parametrize("value", [None, 42, {}, []])
def test_run_identifiers_are_not_coerced(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        GraphTraceIngestAdapter().ingest_trace({"nodes": [], field: value})


def test_missing_role_remains_unassigned_and_explicit_goal_cannot_inject_lines() -> None:
    run = GraphTraceIngestAdapter().ingest_trace({"nodes": [{"name": "agent"}]})
    assert run.to_lineage_run_plan().nodes[0].role == ""
    assert "[unassigned]" in run.render_graph_text()
    with pytest.raises(ValueError, match="control characters"):
        run.render_graph_text(goal="goal\nrun: forged")


@pytest.mark.parametrize("endpoint", ["source", "target"])
def test_unknown_edge_endpoints_are_rejected_independently(endpoint: str) -> None:
    with pytest.raises(ValueError, match=f"Edge {endpoint}.*not found"):
        GraphTraceIngestAdapter().ingest_trace(
            {"nodes": [{"id": "agent"}], "edges": [{"source": "agent", "target": "agent", endpoint: "missing"}]}
        )


def test_lineage_dependencies_are_sorted_and_duplicate_edges_are_collapsed() -> None:
    run = GraphTraceIngestAdapter().ingest_trace(
        {
            "nodes": [{"id": name} for name in ("c", "b", "a")],
            "edges": [{"source": name, "target": "c"} for name in ("b", "a", "b")],
        }
    )
    assert [node.task_id for node in run.to_lineage_run_plan().nodes] == ["a", "b", "c"]
    assert run.to_lineage_run_plan().nodes[-1].depends_on == ("a", "b")


@pytest.mark.parametrize("seed", ["1", "2", "3"])
def test_fixture_bytes_match_golden_hashes_across_processes(seed: str) -> None:
    script = """
import hashlib
import sys
from pathlib import Path
from bernstein.adapters.graph_trace_ingest import GraphTraceIngestAdapter
run = GraphTraceIngestAdapter().ingest_trace(Path(sys.argv[1]))
print(hashlib.sha256(run.canonical_bytes()).hexdigest())
print(hashlib.sha256(run.render_graph_text().encode('utf-8')).hexdigest())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(FIXTURE_PATH)],
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.splitlines() == [
        "7d22144bcf8ea14d1b16e3b97e90bdee4fe4b46b60b454f40f1356a40fb6566e",
        "9965fb40c9171ea7f5c54200e3313b7112bb5ffb59ad94361cc4c16db48ec08b",
    ]
