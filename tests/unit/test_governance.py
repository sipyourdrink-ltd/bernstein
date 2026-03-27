"""Tests for adaptive governance — weight adjustment and decision logging."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.evolution.governance import (
    AdaptiveGovernor,
    EvolutionWeights,
    GovernanceEntry,
    ProjectContext,
    log_evolution_decision,
)

# ---------------------------------------------------------------------------
# EvolutionWeights
# ---------------------------------------------------------------------------


def test_evolution_weights_default_sum_to_one() -> None:
    """Default weights must sum to 1.0 (within float tolerance)."""
    w = EvolutionWeights()
    total = w.test_coverage + w.lint_score + w.type_safety + w.performance + w.security + w.maintainability
    assert abs(total - 1.0) < 1e-9


def test_evolution_weights_normalized_sums_to_one() -> None:
    """normalized() returns weights that sum to 1.0 even when source doesn't."""
    w = EvolutionWeights(
        test_coverage=0.6,
        lint_score=0.6,
        type_safety=0.6,
        performance=0.6,
        security=0.6,
        maintainability=0.6,
    )
    n = w.normalized()
    total = n.test_coverage + n.lint_score + n.type_safety + n.performance + n.security + n.maintainability
    assert abs(total - 1.0) < 1e-9


def test_evolution_weights_all_fields_non_negative() -> None:
    w = EvolutionWeights(
        test_coverage=0.5,
        lint_score=0.1,
        type_safety=0.1,
        performance=0.1,
        security=0.1,
        maintainability=0.1,
    )
    assert w.test_coverage >= 0
    assert w.lint_score >= 0
    assert w.type_safety >= 0
    assert w.performance >= 0
    assert w.security >= 0
    assert w.maintainability >= 0


def test_evolution_weights_to_dict_round_trip() -> None:
    """to_dict / from_dict should preserve all fields."""
    w = EvolutionWeights(
        test_coverage=0.25,
        lint_score=0.20,
        type_safety=0.15,
        performance=0.10,
        security=0.20,
        maintainability=0.10,
    )
    d = w.to_dict()
    w2 = EvolutionWeights.from_dict(d)
    assert w2.test_coverage == pytest.approx(0.25)
    assert w2.security == pytest.approx(0.20)
    assert w2.maintainability == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# ProjectContext
# ---------------------------------------------------------------------------


def test_project_context_fields() -> None:
    ctx = ProjectContext(
        cycle_number=5,
        test_pass_rate=0.95,
        lint_violations=3,
        security_issues_last_5_cycles=2,
        codebase_size_files=150,
        consecutive_empty_cycles=0,
    )
    assert ctx.cycle_number == 5
    assert ctx.test_pass_rate == 0.95
    assert ctx.lint_violations == 3


# ---------------------------------------------------------------------------
# AdaptiveGovernor.adjust_weights
# ---------------------------------------------------------------------------


def test_adjust_weights_boosts_security_when_issues_found(tmp_path: Path) -> None:
    """When security_issues_last_5_cycles > 1, security weight should increase."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    initial = EvolutionWeights()
    ctx = ProjectContext(
        cycle_number=10,
        test_pass_rate=0.92,
        lint_violations=0,
        security_issues_last_5_cycles=3,
        codebase_size_files=200,
        consecutive_empty_cycles=0,
    )
    new_weights, reason = governor.adjust_weights(initial, ctx)
    assert new_weights.security > initial.security
    assert "security" in reason.lower()


def test_adjust_weights_boosts_test_coverage_when_low(tmp_path: Path) -> None:
    """When test_pass_rate < 0.7, test_coverage weight should increase."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    initial = EvolutionWeights()
    ctx = ProjectContext(
        cycle_number=2,
        test_pass_rate=0.55,
        lint_violations=0,
        security_issues_last_5_cycles=0,
        codebase_size_files=80,
        consecutive_empty_cycles=0,
    )
    new_weights, reason = governor.adjust_weights(initial, ctx)
    assert new_weights.test_coverage > initial.test_coverage
    assert "test" in reason.lower()


def test_adjust_weights_boosts_lint_when_many_violations(tmp_path: Path) -> None:
    """When lint_violations > 10, lint_score weight should increase."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    initial = EvolutionWeights()
    ctx = ProjectContext(
        cycle_number=7,
        test_pass_rate=0.98,
        lint_violations=25,
        security_issues_last_5_cycles=0,
        codebase_size_files=300,
        consecutive_empty_cycles=0,
    )
    new_weights, reason = governor.adjust_weights(initial, ctx)
    assert new_weights.lint_score > initial.lint_score
    assert "lint" in reason.lower()


def test_adjust_weights_result_normalized(tmp_path: Path) -> None:
    """Adjusted weights must always sum to 1.0."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    initial = EvolutionWeights()
    ctx = ProjectContext(
        cycle_number=20,
        test_pass_rate=0.40,
        lint_violations=50,
        security_issues_last_5_cycles=5,
        codebase_size_files=500,
        consecutive_empty_cycles=2,
    )
    new_weights, _ = governor.adjust_weights(initial, ctx)
    total = (
        new_weights.test_coverage
        + new_weights.lint_score
        + new_weights.type_safety
        + new_weights.performance
        + new_weights.security
        + new_weights.maintainability
    )
    assert abs(total - 1.0) < 1e-9


def test_adjust_weights_stable_context_minimal_change(tmp_path: Path) -> None:
    """No pressing issues → weights should stay close to defaults."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    initial = EvolutionWeights()
    ctx = ProjectContext(
        cycle_number=15,
        test_pass_rate=0.99,
        lint_violations=0,
        security_issues_last_5_cycles=0,
        codebase_size_files=100,
        consecutive_empty_cycles=0,
    )
    new_weights, _ = governor.adjust_weights(initial, ctx)
    # No individual weight should shift by more than 0.15 in a calm state
    assert abs(new_weights.test_coverage - initial.test_coverage) < 0.15
    assert abs(new_weights.security - initial.security) < 0.15


# ---------------------------------------------------------------------------
# AdaptiveGovernor.log_decision and persistence
# ---------------------------------------------------------------------------


def test_governance_log_written_to_file(tmp_path: Path) -> None:
    """log_decision must append a valid JSON line to governance_log.jsonl."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    entry = GovernanceEntry(
        cycle=1,
        timestamp="2026-03-28T17:00:00Z",
        weights_before={"test_coverage": 0.30, "security": 0.15},
        weights_after={"test_coverage": 0.20, "security": 0.30},
        weight_change_reason="3 security issues found",
        proposals_evaluated=5,
        proposals_applied=2,
        risk_scores=[0.12, 0.08, 0.45, 0.67, 0.23],
        outcome_metrics={"pps_delta": 0.04, "srs_delta": -0.12},
    )
    governor.log_decision(entry)

    log_path = tmp_path / "metrics" / "governance_log.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["cycle"] == 1
    assert parsed["weight_change_reason"] == "3 security issues found"
    assert parsed["proposals_evaluated"] == 5


def test_governance_log_appends_multiple_entries(tmp_path: Path) -> None:
    """Multiple calls to log_decision should append distinct lines."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    for i in range(3):
        entry = GovernanceEntry(
            cycle=i + 1,
            timestamp=f"2026-03-28T17:0{i}:00Z",
            weights_before={},
            weights_after={},
            weight_change_reason="test",
            proposals_evaluated=1,
            proposals_applied=0,
            risk_scores=[0.1],
            outcome_metrics={},
        )
        governor.log_decision(entry)

    log_path = tmp_path / "metrics" / "governance_log.jsonl"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 3
    cycles = [json.loads(l)["cycle"] for l in lines]
    assert cycles == [1, 2, 3]


# ---------------------------------------------------------------------------
# AdaptiveGovernor.persist_weights / get_current_weights
# ---------------------------------------------------------------------------


def test_persist_weights_and_reload(tmp_path: Path) -> None:
    """Weights saved by persist_weights are retrievable by get_current_weights."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    w = EvolutionWeights(
        test_coverage=0.20,
        lint_score=0.20,
        type_safety=0.20,
        performance=0.10,
        security=0.20,
        maintainability=0.10,
    )
    governor.persist_weights(w, reason="initial setup")
    loaded = governor.get_current_weights()
    assert loaded.test_coverage == pytest.approx(0.20)
    assert loaded.security == pytest.approx(0.20)


def test_get_current_weights_returns_defaults_when_no_history(tmp_path: Path) -> None:
    """With no persisted history, returns default EvolutionWeights."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    w = governor.get_current_weights()
    assert isinstance(w, EvolutionWeights)
    # Should be the defaults
    assert w.test_coverage == pytest.approx(0.30)


def test_persist_weights_appended_to_jsonl(tmp_path: Path) -> None:
    """Each persist_weights call appends a line to evolution_weights.jsonl."""
    governor = AdaptiveGovernor(state_dir=tmp_path)
    w1 = EvolutionWeights(
        test_coverage=0.30,
        lint_score=0.15,
        type_safety=0.15,
        performance=0.10,
        security=0.15,
        maintainability=0.15,
    )
    w2 = EvolutionWeights(
        test_coverage=0.20,
        lint_score=0.15,
        type_safety=0.15,
        performance=0.10,
        security=0.25,
        maintainability=0.15,
    )
    governor.persist_weights(w1, reason="cycle 1")
    governor.persist_weights(w2, reason="cycle 2")

    weights_path = tmp_path / "metrics" / "evolution_weights.jsonl"
    lines = weights_path.read_text().strip().splitlines()
    assert len(lines) == 2
    last = json.loads(lines[-1])
    assert last["weights"]["security"] == pytest.approx(0.25)
    assert last["reason"] == "cycle 2"


# ---------------------------------------------------------------------------
# log_evolution_decision — standalone function
# ---------------------------------------------------------------------------


def test_log_evolution_decision_writes_valid_jsonl(tmp_path: Path) -> None:
    """log_evolution_decision writes a parseable JSONL record with all fields."""
    before = EvolutionWeights()
    after = EvolutionWeights(
        test_coverage=0.20,
        lint_score=0.15,
        type_safety=0.15,
        performance=0.10,
        security=0.25,
        maintainability=0.15,
    )
    log_evolution_decision(
        state_dir=tmp_path,
        cycle=3,
        weights_before=before,
        weights_after=after,
        weight_change_reason="security spike",
        proposals_evaluated=7,
        proposals_applied=4,
        risk_scores=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
        outcome_metrics={"pps_delta": 0.05, "srs_delta": -0.10},
    )

    log_path = tmp_path / "metrics" / "governance_log.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["cycle"] == 3
    assert record["proposals_evaluated"] == 7
    assert record["proposals_applied"] == 4
    assert record["weight_change_reason"] == "security spike"
    assert record["weights_before"]["test_coverage"] == pytest.approx(0.30)
    assert record["weights_after"]["security"] == pytest.approx(0.25)
    assert record["risk_scores"] == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    assert record["outcome_metrics"]["pps_delta"] == pytest.approx(0.05)
    assert record["outcome_metrics"]["srs_delta"] == pytest.approx(-0.10)


def test_log_evolution_decision_custom_timestamp(tmp_path: Path) -> None:
    """Explicit timestamp is preserved verbatim in the log record."""
    ts = "2026-01-15T10:30:00+00:00"
    log_evolution_decision(
        state_dir=tmp_path,
        cycle=1,
        weights_before=EvolutionWeights(),
        weights_after=EvolutionWeights(),
        weight_change_reason="none",
        proposals_evaluated=0,
        proposals_applied=0,
        risk_scores=[],
        outcome_metrics={},
        timestamp=ts,
    )

    log_path = tmp_path / "metrics" / "governance_log.jsonl"
    record = json.loads(log_path.read_text().strip())
    assert record["timestamp"] == ts


def test_log_evolution_decision_auto_timestamp_accuracy(tmp_path: Path) -> None:
    """Auto-generated timestamp is valid ISO 8601 and within 5 seconds of now."""
    before_call = datetime.now(UTC)
    log_evolution_decision(
        state_dir=tmp_path,
        cycle=1,
        weights_before=EvolutionWeights(),
        weights_after=EvolutionWeights(),
        weight_change_reason="auto ts test",
        proposals_evaluated=1,
        proposals_applied=1,
        risk_scores=[0.5],
        outcome_metrics={"pps_delta": 0.0},
    )
    after_call = datetime.now(UTC)

    log_path = tmp_path / "metrics" / "governance_log.jsonl"
    record = json.loads(log_path.read_text().strip())
    ts_str = record["timestamp"]

    # Must parse without error (valid ISO 8601)
    ts = datetime.fromisoformat(ts_str)

    # Make both naive for comparison if needed
    if ts.tzinfo is None:
        before_call = before_call.replace(tzinfo=None)
        after_call = after_call.replace(tzinfo=None)

    assert before_call <= ts <= after_call


def test_governance_log_concurrent_writes(tmp_path: Path) -> None:
    """Concurrent calls to log_evolution_decision must not drop any entries."""
    n_threads = 20
    errors: list[Exception] = []

    def write_entry(i: int) -> None:
        try:
            log_evolution_decision(
                state_dir=tmp_path,
                cycle=i,
                weights_before=EvolutionWeights(),
                weights_after=EvolutionWeights(),
                weight_change_reason=f"concurrent write {i}",
                proposals_evaluated=i,
                proposals_applied=0,
                risk_scores=[float(i) * 0.01],
                outcome_metrics={"pps_delta": 0.0, "srs_delta": 0.0},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_entry, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions during concurrent writes: {errors}"

    log_path = tmp_path / "metrics" / "governance_log.jsonl"
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == n_threads

    # Every line must be valid JSON
    for line in lines:
        record = json.loads(line)
        assert "cycle" in record
        assert "timestamp" in record
