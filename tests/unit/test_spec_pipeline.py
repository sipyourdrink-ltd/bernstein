"""Acceptance tests for the spec-to-task-graph pipeline (issue #2361).

The pipeline turns a requirements document into a gated task graph in three
stages with exactly one model call: draft (structured extraction), approve
(the requirement-set hash is bound into the audit chain), and compile (a pure,
deterministic transformation from approved requirements to a task graph).

These tests pin the five acceptance criteria:

1. The same approved requirement set always compiles to a byte-identical task
   graph (hash-asserted).
2. Every task node carries at least one requirement hash, so every artefact a
   node produces traces back to spec lines through lineage.
3. Editing one requirement re-plans only the affected subgraph; unaffected
   tasks retain their content-addressed identity.
4. The approval receipt binds the requirement-set hash into the audit chain.
5. Exactly one model call happens in the pipeline, verified by a counting
   drafter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_SPEC_REQUIREMENT_SET,
    AuditChainStore,
)
from bernstein.sdd.spec_pipeline import (
    EarsKind,
    InstrumentedDrafter,
    RequirementSet,
    StructuralDrafter,
    TaskGraph,
    approve_requirement_set,
    build_requirement_set,
    classify_ears,
    compile_requirements,
    draft_requirements,
    graph_delta,
    hash_text,
    lineage_coverage,
)

_SPEC = """# Password reset

## Acceptance criteria

- [ ] When the user requests a reset, the system shall send a reset email.
- [ ] While a reset token is active, the system shall reject a second request.
- [ ] The system shall expire a reset token after 30 minutes.
- [ ] If the token is expired, then the system shall refuse the reset.
"""


# ---------------------------------------------------------------------------
# Requirement hashing + EARS classification
# ---------------------------------------------------------------------------


def test_hash_text_is_whitespace_canonical() -> None:
    """Canonicalisation collapses internal whitespace and strips ends."""
    assert hash_text("the  system   shall x") == hash_text("the system shall x")
    assert hash_text("  the system shall x  ") == hash_text("the system shall x")
    assert hash_text("the system shall x").startswith("sha256:")


def test_hash_text_distinguishes_meaningful_edits() -> None:
    assert hash_text("the system shall send an email") != hash_text("the system shall send an sms")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("When x happens, the system shall y.", EarsKind.EVENT),
        ("While x holds, the system shall y.", EarsKind.STATE),
        ("Where x is present, the system shall y.", EarsKind.OPTION),
        ("If x, then the system shall y.", EarsKind.UNWANTED),
        ("The system shall y.", EarsKind.UBIQUITOUS),
        ("This line has no modal verb.", EarsKind.UNKNOWN),
    ],
)
def test_classify_ears(text: str, kind: EarsKind) -> None:
    assert classify_ears(text) is kind


# ---------------------------------------------------------------------------
# Draft stage
# ---------------------------------------------------------------------------


def test_structural_drafter_extracts_acceptance_lines() -> None:
    lines = StructuralDrafter()(_SPEC)
    assert len(lines) == 4
    assert all("shall" in line.lower() for line in lines)
    # Markdown checkbox / bullet prefixes are stripped.
    assert not any(line.startswith("- [ ]") for line in lines)


def test_build_requirement_set_assigns_stable_ids_and_hashes() -> None:
    lines = StructuralDrafter()(_SPEC)
    req_set = build_requirement_set(lines, source_text=_SPEC)
    assert isinstance(req_set, RequirementSet)
    assert [r.id for r in req_set.requirements] == ["R001", "R002", "R003", "R004"]
    assert all(r.line_hash.startswith("sha256:") for r in req_set.requirements)
    assert req_set.set_hash.startswith("sha256:")
    assert req_set.source_hash == hash_text(_SPEC)


def test_draft_requirements_calls_model_exactly_once() -> None:
    """AC5: draft is the only stage that can invoke a drafter, and only once."""
    drafter = InstrumentedDrafter(StructuralDrafter())
    req_set = draft_requirements(_SPEC, drafter)
    assert drafter.calls == 1
    # Compiling the drafted set must not invoke the drafter again.
    compile_requirements(req_set)
    assert drafter.calls == 1


# ---------------------------------------------------------------------------
# Compile stage (AC1 determinism, AC2 coverage)
# ---------------------------------------------------------------------------


def test_compile_is_deterministic_and_hash_asserted() -> None:
    """AC1: the same approved requirement set compiles to an identical graph."""
    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph_a = compile_requirements(req_set)
    graph_b = compile_requirements(req_set)
    assert isinstance(graph_a, TaskGraph)
    assert graph_a.graph_hash == graph_b.graph_hash
    assert graph_a.to_dict() == graph_b.to_dict()
    assert [n.node_id for n in graph_a.nodes] == [n.node_id for n in graph_b.nodes]


def test_compile_emits_valid_plan_dict() -> None:
    from bernstein.core.planning.plan_schema import validate_plan

    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph = compile_requirements(req_set)
    plan = graph.to_plan_dict(name="password-reset")
    assert validate_plan(plan) == []


def test_every_node_carries_a_requirement_hash() -> None:
    """AC2: every task node traces back to >=1 requirement hash."""
    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph = compile_requirements(req_set)
    for node in graph.nodes:
        assert node.requirement_hashes
        assert all(h.startswith("sha256:") for h in node.requirement_hashes)
    coverage = lineage_coverage(graph)
    assert coverage.covered
    assert coverage.uncovered_node_ids == ()


def test_lineage_coverage_flags_uncovered_node() -> None:
    from dataclasses import replace

    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph = compile_requirements(req_set)
    broken_node = replace(graph.nodes[0], requirement_hashes=())
    broken = replace(graph, nodes=(broken_node, *graph.nodes[1:]))
    coverage = lineage_coverage(broken)
    assert not coverage.covered
    assert coverage.uncovered_node_ids == (broken_node.node_id,)


# ---------------------------------------------------------------------------
# Graph delta (AC3)
# ---------------------------------------------------------------------------


def test_editing_one_requirement_replans_only_that_node() -> None:
    """AC3: unaffected tasks retain identity; only the edited subgraph changes."""
    lines = StructuralDrafter()(_SPEC)
    old_graph = compile_requirements(build_requirement_set(lines, source_text=_SPEC))

    edited = list(lines)
    edited[2] = "The system shall expire a reset token after 15 minutes."
    new_graph = compile_requirements(build_requirement_set(edited, source_text=_SPEC))

    delta = graph_delta(old_graph, new_graph)
    # Exactly one node added and one removed (the edited requirement).
    assert len(delta.added) == 1
    assert len(delta.removed) == 1
    # The other three nodes keep byte-identical identity.
    assert len(delta.unchanged) == 3
    old_ids = {n.node_id for n in old_graph.nodes}
    new_ids = {n.node_id for n in new_graph.nodes}
    assert set(delta.unchanged) == old_ids & new_ids


def test_graph_delta_empty_when_unchanged() -> None:
    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph = compile_requirements(req_set)
    delta = graph_delta(graph, compile_requirements(req_set))
    assert delta.is_empty
    assert delta.added == ()
    assert delta.removed == ()


# ---------------------------------------------------------------------------
# Approval receipt (AC4)
# ---------------------------------------------------------------------------


def test_approval_receipt_binds_requirement_set_hash(tmp_path: Path) -> None:
    """AC4: the approval receipt binds the requirement-set hash into the chain."""
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph = compile_requirements(req_set)

    receipt, event = approve_requirement_set(chain=chain, req_set=req_set, graph=graph)

    assert event.event_type == EVENT_SPEC_REQUIREMENT_SET
    assert event.resource_id == req_set.set_hash
    assert event.details["requirement_set_hash"] == req_set.set_hash
    assert event.details["graph_hash"] == graph.graph_hash
    assert event.details["decision"] == "approved"
    assert event.details["requirement_count"] == 4
    assert "prev_chain_digest" in event.details
    assert receipt.requirement_set_hash == req_set.set_hash
    assert receipt.graph_hash == graph.graph_hash

    ok, errors = chain.verify()
    assert ok, errors
    rows = chain.query(event_type=EVENT_SPEC_REQUIREMENT_SET)
    assert len(rows) == 1


def test_approval_receipt_survives_tamper_detection(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    req_set = draft_requirements(_SPEC, StructuralDrafter())
    graph = compile_requirements(req_set)
    approve_requirement_set(chain=chain, req_set=req_set, graph=graph)

    log_files = list((tmp_path / "audit").glob("*.jsonl"))
    assert log_files
    target = log_files[0]
    raw = target.read_text(encoding="utf-8")
    tampered = raw.replace(req_set.set_hash, "sha256:" + "f" * 64)
    assert tampered != raw
    target.write_text(tampered, encoding="utf-8")

    ok, _errors = chain.verify()
    assert not ok
