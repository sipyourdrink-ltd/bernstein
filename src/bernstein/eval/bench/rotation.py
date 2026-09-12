"""
bernstein-bench: suite saturation tracking and rotation detection.

A fixed public suite saturates over time; after saturation, scores reflect
overfitting or benchmark-specific tuning rather than general capability.
When the public-set pass rate exceeds 0.9 across three consecutive baselines,
rotation is due.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.leaderboard import LeaderboardEntry


# ---------------------------------------------------------------------------
# Rotation Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationStatus:
    """The saturation and rotation status for a benchmark suite."""

    rotation_due: bool
    consecutive_count: int
    threshold: float
    recent_pass_rates: tuple[float, ...]
    reason: str


# ---------------------------------------------------------------------------
# Saturation & Rotation Check
# ---------------------------------------------------------------------------


def check_suite_saturation(
    baselines: Sequence[SubmissionBundle | LeaderboardEntry | Any],
    threshold: float = 0.9,
    consecutive_required: int = 3,
) -> RotationStatus:
    """
    Check if a suite has saturated across consecutive baselines.

    When the pass rate exceeds *threshold* across *consecutive_required* (default 3)
    consecutive baseline submissions, rotation is flagged as due.
    """
    if not baselines:
        return RotationStatus(
            rotation_due=False,
            consecutive_count=0,
            threshold=threshold,
            recent_pass_rates=(),
            reason="No baseline runs available to assess saturation.",
        )

    # Sort baselines by submitted_at ascending to analyze chronological progression
    sorted_baselines = sorted(baselines, key=lambda b: getattr(b, "submitted_at", 0.0))

    # Extract pass rates
    pass_rates: list[float] = []
    for b in sorted_baselines:
        rate = getattr(b, "pass_rate", getattr(b, "overall_score", 0.0))
        pass_rates.append(float(rate))

    # Count trailing consecutive baselines that exceed the threshold
    consecutive_count = 0
    for rate in reversed(pass_rates):
        if rate >= threshold:
            consecutive_count += 1
        else:
            break

    recent_slice = tuple(pass_rates[-consecutive_count:]) if consecutive_count > 0 else ()
    rotation_due = consecutive_count >= consecutive_required

    if rotation_due:
        reason = (
            f"Suite saturated: pass rate exceeded {threshold:.0%} across "
            f"{consecutive_count} consecutive baselines (threshold: {consecutive_required}). "
            "Suite rotation to new task distribution or holdout promotion is due."
        )
    else:
        reason = (
            f"Public suite not saturated: {consecutive_count}/{consecutive_required} "
            f"recent baselines exceed {threshold:.0%} pass rate."
        )

    return RotationStatus(
        rotation_due=rotation_due,
        consecutive_count=consecutive_count,
        threshold=threshold,
        recent_pass_rates=recent_slice,
        reason=reason,
    )
