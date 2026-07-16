"""Tests for the deterministic BLOCKER -> clearance-gate projection (#2556).

The projection is a pure function of ``(ordered bulletin journal prefix,
blocker content hash, scope)`` onto a canonical ``(clearance_task_id,
injected_edge_set, graph_delta_hash)``. No wall-clock, no RNG: two operators
replaying the same journal must produce byte-identical gates.
"""

from __future__ import annotations

from bernstein.core.communication.bulletin import BulletinMessage
from bernstein.core.communication.signal_actions import (
    ACTION_MATERIALIZE_CLEARANCE_GATE,
    ACTION_OBSERVE,
    ClearanceStatus,
    action_for,
    blocker_content_hash,
    compute_graph_delta_hash,
    journal_prefix_hash,
    project_clearance_gate,
)


def _blocker(content: str = "shared db migration broke", cell_id: str | None = "cell-a") -> BulletinMessage:
    return BulletinMessage(
        agent_id="worker-3",
        type="blocker",
        content=content,
        timestamp=1_700_000_000.0,
        cell_id=cell_id,
    )


# ---------------------------------------------------------------------------
# Signal action registry (Phase 1)
# ---------------------------------------------------------------------------


def test_registry_blocker_materializes_and_others_observe() -> None:
    assert action_for("blocker") == ACTION_MATERIALIZE_CLEARANCE_GATE
    for observe_type in ("alert", "finding", "status", "dependency"):
        assert action_for(observe_type) == ACTION_OBSERVE


def test_registry_unknown_type_defaults_to_observe() -> None:
    # Default is observe so an unrecognised signal never mutates the scheduler.
    assert action_for("totally-new-type") == ACTION_OBSERVE


def test_clearance_status_mirrors_delegation_lifecycle() -> None:
    assert {s.value for s in ClearanceStatus} == {"pending", "cleared", "expired"}


# ---------------------------------------------------------------------------
# Pure projection (Phase 1)
# ---------------------------------------------------------------------------


def test_projection_is_deterministic_across_runs() -> None:
    blocker = _blocker()
    prefix = [BulletinMessage(agent_id="w1", type="status", content="up", timestamp=1.0, cell_id="cell-a"), blocker]
    jph = journal_prefix_hash(prefix)
    scope = ["task-b", "task-a", "task-c"]

    spec1 = project_clearance_gate(blocker=blocker, scope_task_ids=scope, journal_prefix_hash=jph)
    spec2 = project_clearance_gate(blocker=blocker, scope_task_ids=list(reversed(scope)), journal_prefix_hash=jph)

    assert spec1.clearance_task_id == spec2.clearance_task_id
    assert spec1.injected_edges == spec2.injected_edges
    assert spec1.graph_delta_hash == spec2.graph_delta_hash
    # Injected edges are sorted + de-duplicated (canonical set).
    assert spec1.injected_edges == ("task-a", "task-b", "task-c")


def test_clearance_task_id_disambiguated_by_journal_prefix() -> None:
    # Two identical blockers at different journal positions get distinct gates.
    blocker = _blocker()
    jph_a = journal_prefix_hash([blocker])
    jph_b = journal_prefix_hash(
        [BulletinMessage(agent_id="x", type="status", content="noise", timestamp=0.5, cell_id="cell-a"), blocker]
    )
    spec_a = project_clearance_gate(blocker=blocker, scope_task_ids=["t1"], journal_prefix_hash=jph_a)
    spec_b = project_clearance_gate(blocker=blocker, scope_task_ids=["t1"], journal_prefix_hash=jph_b)
    assert spec_a.clearance_task_id != spec_b.clearance_task_id


def test_content_hash_stable_and_prefixed() -> None:
    h = blocker_content_hash(agent_id="worker-3", content="x", cell_id="cell-a")
    assert h.startswith("sha256:")
    assert h == blocker_content_hash(agent_id="worker-3", content="x", cell_id="cell-a")
    assert h != blocker_content_hash(agent_id="worker-3", content="y", cell_id="cell-a")


def test_graph_delta_hash_recomputes_from_recorded_fields() -> None:
    blocker = _blocker()
    jph = journal_prefix_hash([blocker])
    spec = project_clearance_gate(blocker=blocker, scope_task_ids=["t2", "t1"], journal_prefix_hash=jph)
    # A verifier holding only the recorded detail fields recomputes the hash.
    recomputed = compute_graph_delta_hash(
        clearance_task_id=spec.clearance_task_id,
        injected_edges=spec.injected_edges,
        blocker_content_hash=spec.blocker_content_hash,
        scope_cell_id=spec.scope_cell_id,
        deadline=spec.deadline,
    )
    assert recomputed == spec.graph_delta_hash
    # Flipping the blocker content hash diverges the recomputation (tamper).
    tampered = compute_graph_delta_hash(
        clearance_task_id=spec.clearance_task_id,
        injected_edges=spec.injected_edges,
        blocker_content_hash="sha256:" + "0" * 64,
        scope_cell_id=spec.scope_cell_id,
        deadline=spec.deadline,
    )
    assert tampered != spec.graph_delta_hash


def test_deadline_is_deterministic_function_of_inputs_not_wallclock() -> None:
    blocker = _blocker()
    jph = journal_prefix_hash([blocker])
    spec = project_clearance_gate(blocker=blocker, scope_task_ids=["t1"], journal_prefix_hash=jph, ttl_seconds=3600)
    # Deadline is a pure function of the recorded blocker timestamp + ttl.
    assert spec.deadline == int(blocker.timestamp) + 3600
    # No ttl => no expiry deadline.
    spec_no_ttl = project_clearance_gate(blocker=blocker, scope_task_ids=["t1"], journal_prefix_hash=jph)
    assert spec_no_ttl.deadline == 0


def test_empty_scope_yields_no_injected_edges() -> None:
    blocker = _blocker()
    jph = journal_prefix_hash([blocker])
    spec = project_clearance_gate(blocker=blocker, scope_task_ids=[], journal_prefix_hash=jph)
    assert spec.injected_edges == ()
    # A clearance task is still projected so the blocker has an owning artifact.
    assert spec.clearance_task_id.startswith("clearance-")
