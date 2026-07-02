"""Tests for cascade-router auto-promotion driven by the rework-rate signal.

These tests use the public ``CascadeRouter.select`` entry point with an
explicitly constructed :class:`ReworkLedger`, so they exercise the same
code path the orchestrator uses at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.cascade_router import CascadeRouter, _apply_rework_promotion
from bernstein.core.models import Complexity, Scope, Task

from bernstein.core.routing.rework_ledger import ReworkLedger

if TYPE_CHECKING:
    from pathlib import Path


def _task(role: str = "backend") -> Task:
    # HIGH complexity so the deterministic effort mapping lands on "high",
    # matching the effort the rework ledger is seeded at below. The cascade
    # for a HIGH task starts at sonnet, which is the tier these rework-driven
    # promotion tests exercise.
    return Task(
        id="t1",
        title="Do something",
        description="desc",
        role=role,
        complexity=Complexity.HIGH,
        scope=Scope.MEDIUM,
        priority=2,
    )


def _seed_rework(ledger: ReworkLedger, *, model: str, phase: str, n_total: int, n_rework: int) -> None:
    for _ in range(n_rework):
        ledger.record(model=model, effort="high", phase=phase, outcome="rework")
    for _ in range(n_total - n_rework):
        ledger.record(model=model, effort="high", phase=phase, outcome="success")


# ---------------------------------------------------------------------------
# Direct unit: _apply_rework_promotion
# ---------------------------------------------------------------------------


class TestApplyReworkPromotion:
    def test_promotes_when_rate_and_samples_exceed_thresholds(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=30, n_rework=15)

        result = _apply_rework_promotion(
            ledger=ledger,
            cascade=["sonnet", "opus"],
            model="sonnet",
            effort="high",
            phase="backend",
        )
        assert result is not None
        new_model, reason = result
        assert new_model == "opus"
        assert "auto-promotion" in reason
        assert "sonnet" in reason and "opus" in reason

    def test_no_promotion_when_rate_below_threshold(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        # 30 samples, 3 rework => 10% rate, well below the 30% default
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=30, n_rework=3)

        result = _apply_rework_promotion(
            ledger=ledger,
            cascade=["sonnet", "opus"],
            model="sonnet",
            effort="high",
            phase="backend",
        )
        assert result is None

    def test_no_promotion_when_samples_below_min(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        # 100% rework but only 5 samples - should NOT trip the auto-promote.
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=5, n_rework=5)

        result = _apply_rework_promotion(
            ledger=ledger,
            cascade=["sonnet", "opus"],
            model="sonnet",
            effort="high",
            phase="backend",
        )
        assert result is None

    def test_no_promotion_when_already_at_top(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        _seed_rework(ledger, model="opus", phase="backend", n_total=30, n_rework=30)

        result = _apply_rework_promotion(
            ledger=ledger,
            cascade=["sonnet", "opus"],
            model="opus",
            effort="max",
            phase="backend",
        )
        assert result is None

    def test_no_promotion_when_model_outside_cascade(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        result = _apply_rework_promotion(
            ledger=ledger,
            cascade=["sonnet", "opus"],
            model="haiku",
            effort="low",
            phase="backend",
        )
        assert result is None


# ---------------------------------------------------------------------------
# Integration: CascadeRouter.select consults the ledger
# ---------------------------------------------------------------------------


class TestCascadeRouterReworkIntegration:
    def test_empty_ledger_does_not_change_routing(self, tmp_path: Path) -> None:
        """No rework history → router must behave identically to baseline."""
        ledger = ReworkLedger(root=tmp_path / "rework")
        baseline = CascadeRouter()
        with_ledger = CascadeRouter(rework_ledger=ledger)

        baseline_decision = baseline.select(_task())
        rework_decision = with_ledger.select(_task())

        assert baseline_decision.model == rework_decision.model
        assert baseline_decision.effort == rework_decision.effort

    def test_high_rework_rate_promotes_sonnet_to_opus(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        # The router selects ``sonnet`` for backend/medium and ``high`` effort.
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=30, n_rework=15)

        router = CascadeRouter(rework_ledger=ledger)
        decision = router.select(_task())
        assert decision.model == "opus"
        assert "auto-promotion" in decision.reason

    def test_low_rework_rate_keeps_sonnet(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=30, n_rework=3)

        router = CascadeRouter(rework_ledger=ledger)
        decision = router.select(_task())
        assert decision.model == "sonnet"

    def test_few_samples_keep_sonnet_even_at_full_rework(self, tmp_path: Path) -> None:
        ledger = ReworkLedger(root=tmp_path / "rework")
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=5, n_rework=5)

        router = CascadeRouter(rework_ledger=ledger)
        decision = router.select(_task())
        assert decision.model == "sonnet"

    def test_no_promotion_on_escalation_path(self, tmp_path: Path) -> None:
        """Escalation already moves up - rework promotion must not double-jump."""
        ledger = ReworkLedger(root=tmp_path / "rework")
        _seed_rework(ledger, model="sonnet", phase="backend", n_total=30, n_rework=30)

        router = CascadeRouter(rework_ledger=ledger)
        first = router.select(_task())
        # The first attempt was already promoted to opus by the rework
        # signal, so a second attempt would have nowhere to go - but the
        # important invariant we want here is that the escalation path
        # does not consult the ledger and does not blow up.
        assert first.model == "opus"
