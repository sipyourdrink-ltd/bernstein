"""Integration tests for AdaptiveGovernor integration in EvolutionLoop (task 369-04).

Validates:
- Weight adjustment timing (before each scoring cycle)
- Risk computation in the pipeline
- Risk-based routing logic (sandbox_verify / standard / fast_track)
- Governance logging (governance_log.jsonl populated)
- Consecutive-empty tracking
- Governor passed from orchestrator
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import RiskAssessment, RollbackPlan
from bernstein.evolution.detector import ImprovementOpportunity, UpgradeCategory
from bernstein.evolution.gate import ApprovalDecision, ApprovalOutcome
from bernstein.evolution.governance import (
    AdaptiveGovernor,
    EvolutionWeights,
    ProjectContext,
)
from bernstein.evolution.loop import EvolutionLoop
from bernstein.evolution.proposals import UpgradeProposal
from bernstein.evolution.types import RiskLevel
from bernstein.evolution.types import SandboxResult as TypesSandboxResult

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    id: str = "test-001",
    risk_level: str = "low",
    confidence: float = 0.95,
    affected_components: list[str] | None = None,
    proposed_change: str = "change",
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test proposal",
        category=UpgradeCategory.POLICY_UPDATE,
        description="desc",
        current_state="current",
        proposed_change=proposed_change,
        benefits=["benefit"],
        risk_assessment=RiskAssessment(
            level=risk_level,
            affected_components=affected_components or [],
        ),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="improve",
        confidence=confidence,
    )


def _make_opportunity(
    *,
    risk_level: str = "low",
    confidence: float = 0.9,
) -> ImprovementOpportunity:
    return ImprovementOpportunity(
        category=UpgradeCategory.POLICY_UPDATE,
        title="Test opp",
        description="desc",
        expected_improvement="improve",
        confidence=confidence,
        risk_level=risk_level,
    )


def _make_sandbox_result(
    *,
    proposal_id: str = "test-001",
    passed: bool = True,
    candidate_score: float = 1.0,
    delta: float = 0.0,
) -> TypesSandboxResult:
    return TypesSandboxResult(
        proposal_id=proposal_id,
        passed=passed,
        tests_passed=5 if passed else 3,
        tests_failed=0 if passed else 2,
        tests_total=5,
        baseline_score=1.0,
        candidate_score=candidate_score,
        delta=delta,
        duration_seconds=1.0,
        log_path="",
    )


def _make_approval_decision(
    *,
    proposal_id: str = "test-001",
    outcome: ApprovalOutcome = ApprovalOutcome.AUTO_APPROVED,
) -> ApprovalDecision:
    return ApprovalDecision(
        proposal_id=proposal_id,
        risk_level=RiskLevel.L0_CONFIG,
        confidence=0.95,
        outcome=outcome,
        reason="auto",
        requires_human=False,
    )


def _make_loop(tmp_path: Path, **kwargs: object) -> EvolutionLoop:
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(exist_ok=True)
    return EvolutionLoop(state_dir, repo_root=tmp_path, **kwargs)


# ---------------------------------------------------------------------------
# 1. AdaptiveGovernor is instantiated when EvolutionLoop is created
# ---------------------------------------------------------------------------


def test_loop_creates_governor_by_default(tmp_path: Path) -> None:
    """EvolutionLoop creates its own AdaptiveGovernor if none is provided."""
    loop = _make_loop(tmp_path)
    assert isinstance(loop._governor, AdaptiveGovernor)


def test_loop_accepts_external_governor(tmp_path: Path) -> None:
    """EvolutionLoop uses the governor passed in the constructor."""
    governor = AdaptiveGovernor(state_dir=tmp_path / ".sdd")
    loop = _make_loop(tmp_path, governor=governor)
    assert loop._governor is governor


def test_loop_initial_weights_loaded_from_governor(tmp_path: Path) -> None:
    """EvolutionLoop loads initial weights from the governor on startup."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()
    governor = AdaptiveGovernor(state_dir=state_dir)
    # Persist custom weights before creating the loop.
    custom_weights = EvolutionWeights(
        test_coverage=0.50,
        lint_score=0.10,
        type_safety=0.10,
        performance=0.10,
        security=0.10,
        maintainability=0.10,
    )
    governor.persist_weights(custom_weights, reason="test")

    loop = _make_loop(tmp_path, governor=governor)
    assert abs(loop._current_weights.test_coverage - 0.50) < 1e-6


# ---------------------------------------------------------------------------
# 2. Weight adjustment is called before each scoring cycle
# ---------------------------------------------------------------------------


def test_weight_adjustment_called_before_scoring_cycle(tmp_path: Path) -> None:
    """adjust_weights() is called once per main (non-creative) run_cycle()."""
    loop = _make_loop(tmp_path)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._governor, "adjust_weights", wraps=loop._governor.adjust_weights) as mock_adjust,
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
    ):
        loop.run_cycle()

    mock_adjust.assert_called_once()


def test_weight_adjustment_receives_project_context(tmp_path: Path) -> None:
    """adjust_weights() receives a ProjectContext with cycle_number populated."""
    loop = _make_loop(tmp_path)
    captured: list[ProjectContext] = []

    def capture_adjust(weights: EvolutionWeights, ctx: ProjectContext) -> tuple[EvolutionWeights, str]:
        captured.append(ctx)
        return weights, "no change"

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._governor, "adjust_weights", side_effect=capture_adjust),
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
    ):
        loop.run_cycle()

    assert len(captured) == 1
    ctx = captured[0]
    assert isinstance(ctx, ProjectContext)
    assert ctx.cycle_number == 1  # first cycle


# ---------------------------------------------------------------------------
# 3. Risk computation in the pipeline
# ---------------------------------------------------------------------------


def test_risk_score_computed_for_proposal(tmp_path: Path) -> None:
    """_compute_proposal_risk() returns a ProposalRiskScore with composite_risk in [0,1]."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal(affected_components=["src/bernstein/core/orchestrator.py"])
    score = loop._compute_proposal_risk(proposal)
    assert 0.0 <= score.composite_risk <= 1.0


def test_risk_score_core_file_is_high(tmp_path: Path) -> None:
    """Proposals touching core/ files score higher risk than config-only changes."""
    loop = _make_loop(tmp_path)
    core_proposal = _make_proposal(affected_components=["src/bernstein/core/orchestrator.py"])
    config_proposal = _make_proposal(affected_components=["templates/roles/backend.md"])
    core_score = loop._compute_proposal_risk(core_proposal)
    config_score = loop._compute_proposal_risk(config_proposal)
    assert core_score.composite_risk > config_score.composite_risk


def test_risk_score_stored_in_cycle_accumulators(tmp_path: Path) -> None:
    """After proposal generation, risk score appears in _cycle_risk_scores."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(False, "test block")),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
    ):
        loop.run_cycle()

    assert len(loop._cycle_risk_scores) == 1
    assert 0.0 <= loop._cycle_risk_scores[0] <= 1.0


# ---------------------------------------------------------------------------
# 4. Routing logic by risk threshold
# ---------------------------------------------------------------------------


def test_classify_risk_route_high_risk() -> None:
    """composite_risk > 0.6 → sandbox_verify."""
    assert EvolutionLoop._classify_risk_route(0.7) == "sandbox_verify"
    assert EvolutionLoop._classify_risk_route(0.61) == "sandbox_verify"
    assert EvolutionLoop._classify_risk_route(1.0) == "sandbox_verify"


def test_classify_risk_route_medium_risk() -> None:
    """composite_risk (0.3, 0.6] → standard (boundaries exclusive on lower end)."""
    assert EvolutionLoop._classify_risk_route(0.5) == "standard"
    assert EvolutionLoop._classify_risk_route(0.31) == "standard"
    assert EvolutionLoop._classify_risk_route(0.6) == "standard"


def test_classify_risk_route_low_risk() -> None:
    """composite_risk <= 0.3 → fast_track."""
    assert EvolutionLoop._classify_risk_route(0.0) == "fast_track"
    assert EvolutionLoop._classify_risk_route(0.15) == "fast_track"
    assert EvolutionLoop._classify_risk_route(0.29) == "fast_track"
    assert EvolutionLoop._classify_risk_route(0.3) == "fast_track"  # boundary: not > 0.3


def test_fast_track_skips_sandbox(tmp_path: Path) -> None:
    """Proposals with composite_risk < 0.3 bypass sandbox validation."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal()  # default: no affected_components, short change → low risk

    sandbox_called = []

    def mock_validate(**kwargs: object) -> TypesSandboxResult:
        sandbox_called.append(True)
        return _make_sandbox_result()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "")),
        patch.object(loop._gate, "route", return_value=_make_approval_decision()),
        patch.object(loop._eval_gate, "evaluate", return_value=MagicMock(skipped=True, accepted=True)),
        patch.object(loop._sandbox, "validate", side_effect=mock_validate),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
        # Force composite_risk to be low (< 0.3)
        patch.object(loop._risk_scorer, "score_proposal", return_value=MagicMock(composite_risk=0.1)),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert sandbox_called == [], "Sandbox should not be called for fast_track proposals"


def test_sandbox_verify_route_calls_sandbox(tmp_path: Path) -> None:
    """Proposals with composite_risk > 0.6 always go through sandbox validation."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal(
        affected_components=["src/bernstein/core/orchestrator.py"],
        proposed_change="x" * 2000,  # large change → high risk
    )

    sandbox_called = []

    def mock_validate(**kwargs: object) -> TypesSandboxResult:
        sandbox_called.append(True)
        return _make_sandbox_result()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "")),
        patch.object(loop._gate, "route", return_value=_make_approval_decision()),
        patch.object(loop._eval_gate, "evaluate", return_value=MagicMock(skipped=True, accepted=True)),
        patch.object(loop._sandbox, "validate", side_effect=mock_validate),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
        # Force composite_risk to be high (> 0.6)
        patch.object(loop._risk_scorer, "score_proposal", return_value=MagicMock(composite_risk=0.8)),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert len(sandbox_called) == 1, "Sandbox must be called for sandbox_verify proposals"


# ---------------------------------------------------------------------------
# 5. Governance logging populated in .sdd/metrics/governance_log.jsonl
# ---------------------------------------------------------------------------


def test_governance_log_written_on_no_opportunity(tmp_path: Path) -> None:
    """governance_log.jsonl is written even when no opportunities are found."""
    loop = _make_loop(tmp_path)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
    ):
        loop.run_cycle()

    log_path = tmp_path / ".sdd" / "metrics" / "governance_log.jsonl"
    assert log_path.exists(), "governance_log.jsonl must be created"
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["proposals_evaluated"] == 0
    assert entry["proposals_applied"] == 0
    assert entry["risk_scores"] == []


def test_governance_log_written_after_successful_apply(tmp_path: Path) -> None:
    """governance_log.jsonl records applied proposals with risk score."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "")),
        patch.object(loop._gate, "route", return_value=_make_approval_decision()),
        patch.object(loop._eval_gate, "evaluate", return_value=MagicMock(skipped=True, accepted=True)),
        patch.object(loop._sandbox, "validate", return_value=_make_sandbox_result(delta=0.05)),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is True

    log_path = tmp_path / ".sdd" / "metrics" / "governance_log.jsonl"
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["proposals_evaluated"] == 1
    assert entry["proposals_applied"] == 1
    assert len(entry["risk_scores"]) == 1
    assert "delta" in entry["outcome_metrics"]


def test_governance_log_written_after_circuit_break(tmp_path: Path) -> None:
    """governance_log.jsonl is written when circuit breaker blocks a proposal."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(False, "circuit open")),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False
    assert "Circuit breaker" in result.reason

    log_path = tmp_path / ".sdd" / "metrics" / "governance_log.jsonl"
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["proposals_evaluated"] == 1
    assert lines[0]["proposals_applied"] == 0


def test_governance_log_written_after_sandbox_failure(tmp_path: Path) -> None:
    """governance_log.jsonl is written when sandbox validation fails."""
    loop = _make_loop(tmp_path)
    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "")),
        patch.object(loop._gate, "route", return_value=_make_approval_decision()),
        patch.object(loop._sandbox, "validate", return_value=_make_sandbox_result(passed=False)),
        patch.object(loop._breaker, "record_sandbox_failure"),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
        # Force non-fast-track route so sandbox is called
        patch.object(loop._risk_scorer, "score_proposal", return_value=MagicMock(composite_risk=0.5)),
    ):
        result = loop.run_cycle()

    assert result is not None
    assert result.accepted is False

    log_path = tmp_path / ".sdd" / "metrics" / "governance_log.jsonl"
    assert log_path.exists()


# ---------------------------------------------------------------------------
# 6. Consecutive-empty tracking
# ---------------------------------------------------------------------------


def test_consecutive_empty_increments_on_no_opportunity(tmp_path: Path) -> None:
    """_consecutive_empty increments when no opportunities are detected."""
    loop = _make_loop(tmp_path)

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
    ):
        loop.run_cycle()
        loop.run_cycle()

    assert loop._consecutive_empty == 2


def test_consecutive_empty_resets_after_successful_apply(tmp_path: Path) -> None:
    """_consecutive_empty resets to 0 after a proposal is applied."""
    loop = _make_loop(tmp_path)
    loop._consecutive_empty = 5  # seed with a non-zero value

    proposal = _make_proposal()

    with (
        patch.object(loop._aggregator, "run_full_analysis"),
        patch.object(loop._detector, "identify_opportunities", return_value=[_make_opportunity()]),
        patch.object(loop._feature_discovery, "discover", return_value=[]),
        patch.object(loop, "_run_baseline", return_value=1.0),
        patch.object(loop._proposal_generator, "create_proposal", return_value=proposal),
        patch.object(loop._breaker, "can_evolve", return_value=(True, "")),
        patch.object(loop._gate, "route", return_value=_make_approval_decision()),
        patch.object(loop._eval_gate, "evaluate", return_value=MagicMock(skipped=True, accepted=True)),
        patch.object(loop._sandbox, "validate", return_value=_make_sandbox_result()),
        patch.object(loop._executor, "execute_upgrade", return_value=True),
        patch.object(loop._breaker, "record_change"),
        patch.object(loop._governor, "adjust_weights", return_value=(EvolutionWeights(), "no change")),
        patch.object(loop._governor, "persist_weights"),
        patch.object(loop._governor, "log_decision"),
    ):
        loop.run_cycle()

    assert loop._consecutive_empty == 0


# ---------------------------------------------------------------------------
# 7. make_evolution_loop on orchestrator passes the governor
# ---------------------------------------------------------------------------


def test_orchestrator_make_evolution_loop_passes_governor(tmp_path: Path) -> None:
    """Orchestrator.make_evolution_loop() creates an EvolutionLoop with governor wired."""
    from unittest.mock import MagicMock, patch

    from bernstein.core.models import OrchestratorConfig
    from bernstein.core.orchestrator import Orchestrator

    state_dir = tmp_path / ".sdd"
    state_dir.mkdir()

    config = OrchestratorConfig(evolve_mode=True)
    spawner = MagicMock()
    spawner.workdir = tmp_path

    with (
        patch("bernstein.core.orchestration.orchestrator.build_manifest", return_value=MagicMock()),
        patch("bernstein.core.orchestration.orchestrator.save_manifest"),
    ):
        orch = Orchestrator(
            config=config,
            spawner=spawner,
            workdir=tmp_path,
        )

    loop = orch.make_evolution_loop()
    assert isinstance(loop._governor, AdaptiveGovernor)
    # Governor shared with orchestrator (same state_dir)
    assert loop._governor._state_dir == orch._governor._state_dir  # type: ignore[union-attr]
