"""Tests for the eval-gated evolution system (#516).

Tests the EvalGate, EvalGateResult, eval_gate convenience function,
baseline tracking, and trajectory logging.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from bernstein.eval.baseline import (
    EvalBaseline,
    compute_config_hash,
    load_baseline,
    save_baseline,
)
from bernstein.eval.harness import EvalResult, EvalTier
from bernstein.evolution.gate import (
    EVAL_PROMOTION_THRESHOLD,
    EVAL_REGRESSION_TOLERANCE,
    EvalGate,
    EvalGateResult,
    eval_gate,
)
from bernstein.evolution.types import RiskLevel, UpgradeProposal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    id: str = "UPG-TEST-001",
    confidence: float = 0.9,
    risk_level: RiskLevel = RiskLevel.L1_TEMPLATE,
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test eval proposal",
        description="A test eval gate upgrade",
        risk_level=risk_level,
        target_files=["templates/roles/backend.md"],
        diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
        rationale="Testing eval gate",
        expected_impact="Better quality",
        confidence=confidence,
    )


@dataclass
class FakeEvalHarness:
    """Fake eval harness that returns a predetermined score."""

    score: float = 0.80
    components: dict[str, float] | None = None
    fail: bool = False

    def run(self, tier: str = "smoke", sandbox_dir: Path | None = None) -> EvalResult:
        if self.fail:
            raise RuntimeError("Eval harness simulated failure")
        return EvalResult(
            score=self.score,
            components=self.components or {"smoke": self.score},
            tier=tier,
            tasks_evaluated=5,
            duration_seconds=1.0,
        )


# ---------------------------------------------------------------------------
# EvalBaseline
# ---------------------------------------------------------------------------


class TestEvalBaseline:
    def test_save_and_load(self, tmp_path: Path) -> None:
        baseline = EvalBaseline(
            score=0.72,
            components={"smoke": 0.85, "capability": 0.60},
            config_hash="abc123",
        )
        save_baseline(tmp_path, baseline)
        loaded = load_baseline(tmp_path)
        assert loaded is not None
        assert loaded.score == 0.72
        assert loaded.components == {"smoke": 0.85, "capability": 0.60}
        assert loaded.config_hash == "abc123"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "eval" / "baseline.json"
        path.parent.mkdir(parents=True)
        path.write_text("not valid json", encoding="utf-8")
        assert load_baseline(tmp_path) is None

    def test_roundtrip_dict(self) -> None:
        baseline = EvalBaseline(score=0.5, components={"a": 0.5}, config_hash="x")
        data = baseline.to_dict()
        restored = EvalBaseline.from_dict(data)
        assert restored.score == baseline.score
        assert restored.components == baseline.components
        assert restored.config_hash == baseline.config_hash

    def test_compute_config_hash_no_dir(self, tmp_path: Path) -> None:
        assert compute_config_hash(tmp_path) == "no-config"

    def test_compute_config_hash_with_files(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "routing.yaml").write_text("key: value")
        h1 = compute_config_hash(tmp_path)
        assert len(h1) == 12
        # Changing content changes the hash.
        (config_dir / "routing.yaml").write_text("key: different")
        h2 = compute_config_hash(tmp_path)
        assert h1 != h2


# ---------------------------------------------------------------------------
# EvalGateResult
# ---------------------------------------------------------------------------


class TestEvalGateResult:
    def test_to_dict(self) -> None:
        result = EvalGateResult(
            accepted=True,
            score=0.80,
            baseline_score=0.75,
            delta=0.05,
            promoted=False,
            reason="test reason",
            tier="smoke",
        )
        d = result.to_dict()
        assert d["accepted"] is True
        assert d["score"] == 0.80
        assert d["delta"] == 0.05
        assert d["tier"] == "smoke"
        assert d["skipped"] is False

    def test_skipped_result(self) -> None:
        result = EvalGateResult(
            accepted=True,
            score=0.0,
            baseline_score=0.0,
            delta=0.0,
            promoted=False,
            reason="skipped",
            skipped=True,
        )
        assert result.skipped is True
        assert result.accepted is True


# ---------------------------------------------------------------------------
# EvalGate — core decision logic
# ---------------------------------------------------------------------------


class TestEvalGate:
    def setup_method(self) -> None:
        """Common setup: create a tmp dir and baseline."""
        # Note: tmp_path is injected by pytest per-test, but we use a shared pattern
        pass

    def test_l0_skips_eval(self, tmp_path: Path) -> None:
        """L0_CONFIG proposals should skip eval entirely."""
        harness = FakeEvalHarness(score=0.50)  # Would fail if actually called
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L0_CONFIG)

        result = gate.evaluate(proposal, RiskLevel.L0_CONFIG)

        assert result.accepted is True
        assert result.skipped is True
        assert result.tier is None

    def test_l3_skips_eval(self, tmp_path: Path) -> None:
        """L3_STRUCTURAL proposals should skip eval (blocked anyway)."""
        harness = FakeEvalHarness(score=0.50)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L3_STRUCTURAL)

        result = gate.evaluate(proposal, RiskLevel.L3_STRUCTURAL)

        assert result.accepted is True
        assert result.skipped is True

    def test_l1_runs_smoke_eval(self, tmp_path: Path) -> None:
        """L1_TEMPLATE proposals should run smoke eval."""
        harness = FakeEvalHarness(score=0.80)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True
        assert result.skipped is False
        assert result.tier == "smoke"

    def test_l2_runs_standard_eval(self, tmp_path: Path) -> None:
        """L2_LOGIC proposals should run standard eval."""
        harness = FakeEvalHarness(score=0.80)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L2_LOGIC)

        result = gate.evaluate(proposal, RiskLevel.L2_LOGIC)

        assert result.accepted is True
        assert result.tier == "standard"

    def test_accept_above_baseline(self, tmp_path: Path) -> None:
        """Score above baseline should be accepted."""
        save_baseline(tmp_path, EvalBaseline(score=0.70))
        harness = FakeEvalHarness(score=0.75)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True
        assert result.delta == pytest.approx(0.05, abs=1e-6)

    def test_accept_within_tolerance(self, tmp_path: Path) -> None:
        """Score slightly below baseline but within tolerance should be accepted."""
        save_baseline(tmp_path, EvalBaseline(score=0.80))
        harness = FakeEvalHarness(score=0.79)  # 0.01 below, within 0.02 tolerance
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True
        assert result.delta == pytest.approx(-0.01, abs=1e-6)

    def test_accept_at_exact_tolerance_boundary(self, tmp_path: Path) -> None:
        """Score exactly at baseline - tolerance should be accepted."""
        save_baseline(tmp_path, EvalBaseline(score=0.80))
        harness = FakeEvalHarness(score=0.78)  # Exactly at baseline - 0.02
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True

    def test_reject_below_tolerance(self, tmp_path: Path) -> None:
        """Score below baseline - tolerance should be rejected."""
        save_baseline(tmp_path, EvalBaseline(score=0.80))
        harness = FakeEvalHarness(score=0.77)  # 0.03 below, exceeds 0.02 tolerance
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is False
        assert "REJECTED" in result.reason

    def test_promote_baseline_on_large_improvement(self, tmp_path: Path) -> None:
        """Score significantly above baseline should promote the baseline."""
        save_baseline(tmp_path, EvalBaseline(score=0.70))
        harness = FakeEvalHarness(score=0.80)  # 0.10 above, exceeds 0.05 threshold
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True
        assert result.promoted is True

        # Verify baseline was updated.
        new_baseline = load_baseline(tmp_path)
        assert new_baseline is not None
        assert new_baseline.score == 0.80

    def test_no_promote_on_small_improvement(self, tmp_path: Path) -> None:
        """Small improvement should not promote baseline."""
        save_baseline(tmp_path, EvalBaseline(score=0.70))
        harness = FakeEvalHarness(score=0.73)  # 0.03 above, below 0.05 threshold
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True
        assert result.promoted is False

        # Baseline should not change.
        bl = load_baseline(tmp_path)
        assert bl is not None
        assert bl.score == 0.70

    def test_no_baseline_uses_zero(self, tmp_path: Path) -> None:
        """When no baseline exists, use 0.0 as the baseline score."""
        harness = FakeEvalHarness(score=0.60)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is True
        assert result.baseline_score == 0.0
        assert result.delta == pytest.approx(0.60, abs=1e-6)

    def test_harness_failure_rejects(self, tmp_path: Path) -> None:
        """If the eval harness fails, the proposal should be rejected."""
        harness = FakeEvalHarness(fail=True)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)
        proposal = _make_proposal()

        result = gate.evaluate(proposal, RiskLevel.L1_TEMPLATE)

        assert result.accepted is False
        assert "failed" in result.reason.lower()

    def test_custom_thresholds(self, tmp_path: Path) -> None:
        """Custom regression tolerance and promotion threshold should be respected."""
        save_baseline(tmp_path, EvalBaseline(score=0.80))
        harness = FakeEvalHarness(score=0.75)

        # With default tolerance (0.02), 0.75 < 0.78 → rejected
        gate_strict = EvalGate(eval_harness=harness, state_dir=tmp_path)
        result_strict = gate_strict.evaluate(_make_proposal(), RiskLevel.L1_TEMPLATE)
        assert result_strict.accepted is False

        # With generous tolerance (0.10), 0.75 >= 0.70 → accepted
        gate_generous = EvalGate(
            eval_harness=harness,
            state_dir=tmp_path,
            regression_tolerance=0.10,
        )
        result_generous = gate_generous.evaluate(_make_proposal(), RiskLevel.L1_TEMPLATE)
        assert result_generous.accepted is True


# ---------------------------------------------------------------------------
# Trajectory logging
# ---------------------------------------------------------------------------


class TestEvalTrajectory:
    def test_trajectory_logged(self, tmp_path: Path) -> None:
        """Eval gate should append to eval_trajectory.jsonl."""
        save_baseline(tmp_path, EvalBaseline(score=0.70))
        harness = FakeEvalHarness(score=0.75)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)

        gate.evaluate(_make_proposal(id="P-001"), RiskLevel.L1_TEMPLATE)
        gate.evaluate(_make_proposal(id="P-002"), RiskLevel.L1_TEMPLATE)

        trajectory_path = tmp_path / "metrics" / "eval_trajectory.jsonl"
        assert trajectory_path.exists()
        lines = trajectory_path.read_text().strip().splitlines()
        assert len(lines) == 2

        record = json.loads(lines[0])
        assert record["proposal_id"] == "P-001"
        assert record["baseline"] == 0.70
        assert record["proposed"] == 0.75
        assert record["accepted"] is True

    def test_trajectory_not_written_for_skipped(self, tmp_path: Path) -> None:
        """Skipped evals (L0) should not produce trajectory entries."""
        harness = FakeEvalHarness(score=0.80)
        gate = EvalGate(eval_harness=harness, state_dir=tmp_path)

        gate.evaluate(
            _make_proposal(risk_level=RiskLevel.L0_CONFIG),
            RiskLevel.L0_CONFIG,
        )

        trajectory_path = tmp_path / "metrics" / "eval_trajectory.jsonl"
        # File may not exist or be empty — skipped evals don't log.
        if trajectory_path.exists():
            content = trajectory_path.read_text().strip()
            assert content == ""


# ---------------------------------------------------------------------------
# eval_gate convenience function
# ---------------------------------------------------------------------------


class TestEvalGateFunction:
    def test_convenience_function_delegates(self, tmp_path: Path) -> None:
        """The eval_gate() function should delegate to EvalGate.evaluate()."""
        save_baseline(tmp_path, EvalBaseline(score=0.70))
        harness = FakeEvalHarness(score=0.75)
        proposal = _make_proposal()

        result = eval_gate(
            proposal=proposal,
            risk_level=RiskLevel.L1_TEMPLATE,
            eval_harness=harness,
            state_dir=tmp_path,
        )

        assert result.accepted is True
        assert result.score == 0.75
        assert result.baseline_score == 0.70

    def test_convenience_function_rejects(self, tmp_path: Path) -> None:
        """The eval_gate() function should reject below threshold."""
        save_baseline(tmp_path, EvalBaseline(score=0.90))
        harness = FakeEvalHarness(score=0.50)

        result = eval_gate(
            proposal=_make_proposal(),
            risk_level=RiskLevel.L1_TEMPLATE,
            eval_harness=harness,
            state_dir=tmp_path,
        )

        assert result.accepted is False


# ---------------------------------------------------------------------------
# EvalTier enum
# ---------------------------------------------------------------------------


class TestEvalTier:
    def test_smoke_tier(self) -> None:
        assert EvalTier("smoke") == EvalTier.SMOKE

    def test_standard_tier(self) -> None:
        assert EvalTier("standard") == EvalTier.STANDARD

    def test_full_tier(self) -> None:
        assert EvalTier("full") == EvalTier.FULL


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_regression_tolerance(self) -> None:
        assert EVAL_REGRESSION_TOLERANCE == 0.02

    def test_promotion_threshold(self) -> None:
        assert EVAL_PROMOTION_THRESHOLD == 0.05
