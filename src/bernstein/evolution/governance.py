"""Adaptive governance for the evolution system.

Dynamically adjusts metric weights each evolution cycle based on project
context, and maintains a full audit trail of every decision.

Three core responsibilities:
1. EvolutionWeights — the current scoring criteria for proposals.
2. AdaptiveGovernor.adjust_weights — heuristic re-weighting from context.
3. AdaptiveGovernor.log_decision — append governance entry to JSONL trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class EvolutionWeights:
    """Scoring weights for evolution metric dimensions.

    All six fields must sum to 1.0.  Use ``normalized()`` to enforce this
    after any arithmetic modification.
    """

    test_coverage: float = 0.30
    lint_score: float = 0.15
    type_safety: float = 0.15
    performance: float = 0.10
    security: float = 0.15
    maintainability: float = 0.15

    def normalized(self) -> EvolutionWeights:
        """Return a copy with all weights scaled so they sum to exactly 1.0."""
        total = (
            self.test_coverage
            + self.lint_score
            + self.type_safety
            + self.performance
            + self.security
            + self.maintainability
        )
        if abs(total) < 1e-9:
            # Degenerate — return uniform distribution
            sixth = 1.0 / 6.0
            return EvolutionWeights(
                test_coverage=sixth,
                lint_score=sixth,
                type_safety=sixth,
                performance=sixth,
                security=sixth,
                maintainability=sixth,
            )
        return EvolutionWeights(
            test_coverage=self.test_coverage / total,
            lint_score=self.lint_score / total,
            type_safety=self.type_safety / total,
            performance=self.performance / total,
            security=self.security / total,
            maintainability=self.maintainability / total,
        )

    def to_dict(self) -> dict[str, float]:
        """Serialize to a plain dict (for JSONL persistence)."""
        return {
            "test_coverage": self.test_coverage,
            "lint_score": self.lint_score,
            "type_safety": self.type_safety,
            "performance": self.performance,
            "security": self.security,
            "maintainability": self.maintainability,
        }

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> EvolutionWeights:
        """Deserialize from a plain dict."""
        return cls(
            test_coverage=float(d.get("test_coverage", 0.30)),
            lint_score=float(d.get("lint_score", 0.15)),
            type_safety=float(d.get("type_safety", 0.15)),
            performance=float(d.get("performance", 0.10)),
            security=float(d.get("security", 0.15)),
            maintainability=float(d.get("maintainability", 0.15)),
        )


@dataclass
class ProjectContext:
    """Snapshot of project health used to adjust evolution weights.

    Passed to ``AdaptiveGovernor.adjust_weights`` each cycle.
    """

    cycle_number: int
    test_pass_rate: float  # 0.0-1.0
    lint_violations: int
    security_issues_last_5_cycles: int
    codebase_size_files: int
    consecutive_empty_cycles: int


@dataclass
class GovernanceEntry:
    """A single record in the evolution governance trail.

    Written to ``.sdd/metrics/governance_log.jsonl`` each evolution cycle.
    """

    cycle: int
    timestamp: str  # ISO 8601
    weights_before: dict[str, float]
    weights_after: dict[str, float]
    weight_change_reason: str
    proposals_evaluated: int
    proposals_applied: int
    risk_scores: list[float]
    outcome_metrics: dict[str, float]


# ---------------------------------------------------------------------------
# AdaptiveGovernor
# ---------------------------------------------------------------------------

# Nudge applied to a dimension's weight when context signals it matters more.
_BOOST = 0.10
# How much to reduce the other weights when one is boosted (distributed evenly).
_REDUCE_PER_OTHER = _BOOST / 5  # 5 other dimensions


class AdaptiveGovernor:
    """Adjusts evolution metric weights based on project context.

    Persists weight history to ``.sdd/metrics/evolution_weights.jsonl``
    and the full decision trail to ``.sdd/metrics/governance_log.jsonl``.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._metrics_dir = state_dir / "metrics"

    # -- Weight adjustment ---------------------------------------------------

    def adjust_weights(
        self,
        current_weights: EvolutionWeights,
        context: ProjectContext,
    ) -> tuple[EvolutionWeights, str]:
        """Compute adjusted weights from project context.

        Uses heuristic rules derived from the SYNAPSE adaptive-governance
        research: boost the dimension most likely to unblock current progress,
        reduce others proportionally, then normalise.

        Returns:
            A tuple of (new_weights, human-readable reason string).
        """
        reasons: list[str] = []

        # Start from mutable copies
        tc = current_weights.test_coverage
        ls = current_weights.lint_score
        ts = current_weights.type_safety
        pf = current_weights.performance
        sec = current_weights.security
        mt = current_weights.maintainability

        # Rule 1: security issues demand immediate attention
        if context.security_issues_last_5_cycles > 1:
            sec += _BOOST
            # Reduce non-critical dims proportionally
            tc -= _REDUCE_PER_OTHER
            ls -= _REDUCE_PER_OTHER
            pf -= _REDUCE_PER_OTHER
            mt -= _REDUCE_PER_OTHER
            ts -= _REDUCE_PER_OTHER
            reasons.append(f"security: {context.security_issues_last_5_cycles} issues found in last 5 cycles")

        # Rule 2: poor test health — prioritise coverage
        if context.test_pass_rate < 0.70:
            tc += _BOOST
            ls -= _REDUCE_PER_OTHER
            ts -= _REDUCE_PER_OTHER
            pf -= _REDUCE_PER_OTHER
            sec -= _REDUCE_PER_OTHER
            mt -= _REDUCE_PER_OTHER
            reasons.append(f"test coverage: pass rate {context.test_pass_rate:.0%} is below 70%")

        # Rule 3: lint backlog is large
        if context.lint_violations > 10:
            ls += _BOOST
            tc -= _REDUCE_PER_OTHER
            ts -= _REDUCE_PER_OTHER
            pf -= _REDUCE_PER_OTHER
            sec -= _REDUCE_PER_OTHER
            mt -= _REDUCE_PER_OTHER
            reasons.append(f"lint score: {context.lint_violations} violations detected")

        # Clamp negatives to a floor of 0.02 before normalising
        def _floor(v: float) -> float:
            return max(v, 0.02)

        new_weights = EvolutionWeights(
            test_coverage=_floor(tc),
            lint_score=_floor(ls),
            type_safety=_floor(ts),
            performance=_floor(pf),
            security=_floor(sec),
            maintainability=_floor(mt),
        ).normalized()

        reason = "; ".join(reasons) if reasons else "no significant issues detected"
        return new_weights, reason

    # -- Persistence ---------------------------------------------------------

    def persist_weights(self, weights: EvolutionWeights, reason: str) -> None:
        """Append the current weight vector to evolution_weights.jsonl."""
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        path = self._metrics_dir / "evolution_weights.jsonl"
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "weights": weights.to_dict(),
            "reason": reason,
        }
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def get_current_weights(self) -> EvolutionWeights:
        """Return the most recently persisted weights, or defaults if none."""
        path = self._metrics_dir / "evolution_weights.jsonl"
        if not path.exists():
            return EvolutionWeights()
        try:
            lines = path.read_text().strip().splitlines()
            if not lines:
                return EvolutionWeights()
            last = json.loads(lines[-1])
            return EvolutionWeights.from_dict(last["weights"])
        except (OSError, json.JSONDecodeError, KeyError):
            return EvolutionWeights()

    # -- Decision logging ----------------------------------------------------

    def log_decision(self, entry: GovernanceEntry) -> None:
        """Append a governance entry to governance_log.jsonl."""
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        path = self._metrics_dir / "governance_log.jsonl"
        record: dict[str, Any] = {
            "cycle": entry.cycle,
            "timestamp": entry.timestamp,
            "weights_before": entry.weights_before,
            "weights_after": entry.weights_after,
            "weight_change_reason": entry.weight_change_reason,
            "proposals_evaluated": entry.proposals_evaluated,
            "proposals_applied": entry.proposals_applied,
            "risk_scores": entry.risk_scores,
            "outcome_metrics": entry.outcome_metrics,
        }
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def log_evolution_decision(
    state_dir: Path,
    cycle: int,
    weights_before: EvolutionWeights,
    weights_after: EvolutionWeights,
    weight_change_reason: str,
    proposals_evaluated: int,
    proposals_applied: int,
    risk_scores: list[float],
    outcome_metrics: dict[str, float],
    *,
    timestamp: str | None = None,
) -> None:
    """Write one governance decision record to governance_log.jsonl.

    A module-level convenience wrapper around ``AdaptiveGovernor.log_decision``
    that handles ``EvolutionWeights`` serialisation and timestamp generation.

    Args:
        state_dir: Root state directory (e.g. project ``.sdd/`` folder).
        cycle: Current evolution cycle number.
        weights_before: Metric weights active before this cycle's adjustment.
        weights_after: Metric weights active after this cycle's adjustment.
        weight_change_reason: Human-readable reason for the weight change.
        proposals_evaluated: Total proposals reviewed this cycle.
        proposals_applied: Proposals accepted and applied this cycle.
        risk_scores: Per-proposal risk scores in evaluation order.
        outcome_metrics: Post-cycle deltas (``pps_delta``, ``srs_delta``, etc.).
        timestamp: ISO 8601 string; auto-generated from UTC now if *None*.
    """
    ts = timestamp or datetime.now(UTC).isoformat()
    entry = GovernanceEntry(
        cycle=cycle,
        timestamp=ts,
        weights_before=weights_before.to_dict(),
        weights_after=weights_after.to_dict(),
        weight_change_reason=weight_change_reason,
        proposals_evaluated=proposals_evaluated,
        proposals_applied=proposals_applied,
        risk_scores=risk_scores,
        outcome_metrics=outcome_metrics,
    )
    AdaptiveGovernor(state_dir).log_decision(entry)
