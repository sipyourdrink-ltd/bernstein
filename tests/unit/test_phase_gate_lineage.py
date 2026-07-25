"""Phase-gate boundary lineage helpers.

The per-boundary write goes through :class:`LineageSpine` (see
``tests/unit/test_phase_gate_spine_lineage.py`` for the end-to-end wiring).
This module covers the pure projection helper the adjudication hook renders
into its signed record, plus the guard that the retired v1 WAL-backed hook
does not creep back in.
"""

from __future__ import annotations

from bernstein.core.orchestration import phase_gate_lineage
from bernstein.core.orchestration.phase_gate_lineage import gate_results_summary
from bernstein.core.orchestration.phase_gates import GateOutcome, GateResult
from bernstein.core.orchestration.phase_pipeline import Phase


def _result(rule_id: str, outcome: GateOutcome) -> GateResult:
    return GateResult(
        rule_id=rule_id,
        label=rule_id,
        outcome=outcome,
        boundary_from=Phase.RESEARCH,
        boundary_to=Phase.PLAN,
    )


def test_gate_results_summary_includes_per_rule_outcome() -> None:
    summary = gate_results_summary(
        [
            _result("R001-no-open-questions", GateOutcome.PASS),
            _result("R005-byte-budget", GateOutcome.FAIL),
        ]
    )
    rule_ids = {entry["rule_id"] for entry in summary["rules"]}
    assert rule_ids == {"R001-no-open-questions", "R005-byte-budget"}
    outcomes = {entry["outcome"] for entry in summary["rules"]}
    assert outcomes == {"pass", "fail"}


def test_gate_results_summary_carries_the_boundary_per_rule() -> None:
    summary = gate_results_summary([_result("R001-no-open-questions", GateOutcome.PASS)])
    (rule,) = summary["rules"]
    assert rule["boundary_from"] == Phase.RESEARCH.value
    assert rule["boundary_to"] == Phase.PLAN.value


def test_v1_writer_typed_entrypoints_are_retired() -> None:
    """No entrypoint here is typed on the deprecated v1 ``LineageWriter``."""
    for retired in ("make_lineage_hook", "build_phase_gate_record", "PHASE_GATE_REGULATORY_CLASS"):
        assert not hasattr(phase_gate_lineage, retired), f"{retired} must stay retired (issue #2960)"
    assert "make_lineage_hook" not in phase_gate_lineage.__all__
