"""Tests for LangGraph foreign runtime ingest adapter (issue #4964).

Proof assertions from #4964:
- a recorded fixture run ingests to a graph whose nodes and edges match the source;
- every tool call in the fixture appears with a digest of its arguments;
- the rendered graph is byte-identical across two ingests of the same fixture;
- a node present in the fixture but absent from the ingest fails the test rather
  than silently shrinking the graph.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bernstein.adapters.langgraph_ingest import (
    LangGraphCallbackHandler,
    LangGraphIngestAdapter,
    LangGraphIngestPlugin,
)
from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration
from bernstein.plugins.manager import PluginManager

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "govern" / "langgraph_run.json"


@pytest.fixture
def adapter() -> LangGraphIngestAdapter:
    return LangGraphIngestAdapter()


@pytest.fixture
def raw_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_langgraph_fixture_ingests_to_matching_graph(
    adapter: LangGraphIngestAdapter,
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


def test_every_tool_call_has_arguments_digest(
    adapter: LangGraphIngestAdapter,
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
    adapter: LangGraphIngestAdapter,
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
    adapter: LangGraphIngestAdapter,
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
    adapter: LangGraphIngestAdapter,
) -> None:
    """Tool calls with list arguments (raw or JSON string) parse and digest without crash."""
    from bernstein.adapters.langgraph_ingest import LangGraphToolCall

    tc_list = LangGraphToolCall.from_dict({
        "name": "batch_search",
        "id": "call_1",
        "args": [1, 2, 3],
    })
    assert tc_list.arguments == [1, 2, 3]
    assert tc_list.arguments_digest.startswith("sha256:")

    tc_json_list = LangGraphToolCall.from_dict({
        "name": "batch_search",
        "id": "call_2",
        "args": '["a", "b", "c"]',
    })
    assert tc_json_list.arguments == ["a", "b", "c"]
    assert tc_json_list.arguments_digest.startswith("sha256:")


def test_callback_handler_preserves_graph_cycles() -> None:
    """Nodes executing multiple times in loops/cycles are preserved without overwriting."""
    handler = LangGraphCallbackHandler(run_id="run-cycle", graph_id="cycle-graph")

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
    adapter: LangGraphIngestAdapter,
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


def test_langgraph_callback_handler_records_trace(
    adapter: LangGraphIngestAdapter,
) -> None:
    """LangGraphCallbackHandler intercepts execution callbacks into an ingestable trace."""
    handler = LangGraphCallbackHandler(run_id="run-cb-123", graph_id="agent-graph")

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
    plugin = LangGraphIngestPlugin()
    decl = plugin.provide_ingest_adapter()

    assert isinstance(decl, IngestAdapterDeclaration)
    assert decl.name == "langgraph"
    assert "gen_ai_activity" in decl.declared_event_types
    assert "untyped_activity" in decl.declared_event_types

    pm = PluginManager(workdir=tmp_path)
    pm.register(plugin, name="langgraph-ingest-plugin")
    added = pm.collect_plugin_ingest_adapters()
    assert added == 1
