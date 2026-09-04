"""
bernstein-bench: bundle comparison.

Provides side-by-side metric and per-task comparison between two
:class:`~bernstein.eval.bench.bundle.SubmissionBundle` instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle


@dataclass
class TaskComparison:
    """Comparison of a single task across two bundles."""

    task_id: str
    passed_a: bool | None
    passed_b: bool | None
    score_a: float | None
    score_b: float | None
    cost_a: float | None
    cost_b: float | None
    tokens_a: int | None
    tokens_b: int | None
    duration_a: float | None
    duration_b: float | None

    @property
    def verdict_transition(self) -> str:
        va = "PASS" if self.passed_a else ("FAIL" if self.passed_a is not None else "N/A")
        vb = "PASS" if self.passed_b else ("FAIL" if self.passed_b is not None else "N/A")
        return f"{va} -> {vb}"

    @property
    def cost_delta(self) -> float:
        return (self.cost_b or 0.0) - (self.cost_a or 0.0)

    @property
    def token_delta(self) -> int:
        return (self.tokens_b or 0) - (self.tokens_a or 0)

    @property
    def score_delta(self) -> float:
        return (self.score_b or 0.0) - (self.score_a or 0.0)


@dataclass
class BundleComparison:
    """Side-by-side comparison between two submission bundles."""

    bundle_a_hash: str
    bundle_b_hash: str
    score_a: float
    score_b: float
    pass_rate_a: float
    pass_rate_b: float
    total_cost_a: float
    total_cost_b: float
    total_tokens_a: int
    total_tokens_b: int
    total_duration_a: float
    total_duration_b: float
    task_comparisons: list[TaskComparison]

    @property
    def score_delta(self) -> float:
        return self.score_b - self.score_a

    @property
    def pass_rate_delta(self) -> float:
        return self.pass_rate_b - self.pass_rate_a

    @property
    def cost_delta(self) -> float:
        return self.total_cost_b - self.total_cost_a

    @property
    def token_delta(self) -> int:
        return self.total_tokens_b - self.total_tokens_a

    @property
    def duration_delta(self) -> float:
        return self.total_duration_b - self.total_duration_a

    def report(self) -> str:
        """Render a formatted comparison table."""
        cost_pct = f" ({self.cost_delta / self.total_cost_a * 100:+.1f}%)" if self.total_cost_a > 0 else ""
        token_pct = f" ({self.token_delta / self.total_tokens_a * 100:+.1f}%)" if self.total_tokens_a > 0 else ""

        score_a_str = f"{self.score_a * 100:.1f}%"
        score_b_str = f"{self.score_b * 100:.1f}%"
        score_delta_str = f"{self.score_delta * 100:+.1f}%"

        pass_a_str = f"{self.pass_rate_a * 100:.1f}%"
        pass_b_str = f"{self.pass_rate_b * 100:.1f}%"
        pass_delta_str = f"{self.pass_rate_delta * 100:+.1f}%"

        cost_a_str = f"${self.total_cost_a:.4f}"
        cost_b_str = f"${self.total_cost_b:.4f}"
        cost_delta_str = f"${self.cost_delta:+.4f}{cost_pct}"

        tokens_a_str = f"{self.total_tokens_a:,d}"
        tokens_b_str = f"{self.total_tokens_b:,d}"
        tokens_delta_str = f"{self.token_delta:+d}{token_pct}"

        dur_a_str = f"{self.total_duration_a:.2f}s"
        dur_b_str = f"{self.total_duration_b:.2f}s"
        dur_delta_str = f"{self.duration_delta:+.2f}s"

        lines = [
            f"Bundle A : {self.bundle_a_hash}",
            f"Bundle B : {self.bundle_b_hash}",
            "",
            f"{'Metric':<20} {'Bundle A':<15} {'Bundle B':<15} {'Delta':<20}",
            "-" * 70,
            f"{'Overall Score':<20} {score_a_str:<15} {score_b_str:<15} {score_delta_str:<20}",
            f"{'Pass Rate':<20} {pass_a_str:<15} {pass_b_str:<15} {pass_delta_str:<20}",
            f"{'Total Cost':<20} {cost_a_str:<15} {cost_b_str:<15} {cost_delta_str:<20}",
            f"{'Total Tokens':<20} {tokens_a_str:<15} {tokens_b_str:<15} {tokens_delta_str:<20}",
            f"{'Total Duration':<20} {dur_a_str:<15} {dur_b_str:<15} {dur_delta_str:<20}",
            "",
            "Per-task breakdown:",
            f"  {'Task ID':<30} {'Verdict':<15} {'Cost A -> B':<22} {'Tokens A -> B':<20}",
            "  " + "-" * 88,
        ]

        for tc in self.task_positions():
            cost_a_str = f"${tc.cost_a:.4f}" if tc.cost_a is not None else "N/A"
            cost_b_str = f"${tc.cost_b:.4f}" if tc.cost_b is not None else "N/A"
            tokens_a_str = f"{tc.tokens_a}" if tc.tokens_a is not None else "N/A"
            tokens_b_str = f"{tc.tokens_b}" if tc.tokens_b is not None else "N/A"

            cost_str = f"{cost_a_str} -> {cost_b_str}"
            tokens_str = f"{tokens_a_str} -> {tokens_b_str}"

            lines.append(f"  {tc.task_id:<30} {tc.verdict_transition:<15} {cost_str:<22} {tokens_str:<20}")

        return "\n".join(lines)

    def task_positions(self) -> list[TaskComparison]:
        return self.task_comparisons


def compare_bundles(bundle_a: SubmissionBundle, bundle_b: SubmissionBundle) -> BundleComparison:
    """Compare two submission bundles."""
    tasks_a = {r.task_id: r for r in bundle_a.task_results}
    tasks_b = {r.task_id: r for r in bundle_b.task_results}

    all_task_ids = list(dict.fromkeys(list(tasks_a.keys()) + list(tasks_b.keys())))
    task_comparisons: list[TaskComparison] = []

    for tid in all_task_ids:
        ra = tasks_a.get(tid)
        rb = tasks_b.get(tid)

        task_comparisons.append(
            TaskComparison(
                task_id=tid,
                passed_a=ra.passed if ra else None,
                passed_b=rb.passed if rb else None,
                score_a=ra.score if ra else None,
                score_b=rb.score if rb else None,
                cost_a=ra.cost_usd if ra else None,
                cost_b=rb.cost_usd if rb else None,
                tokens_a=ra.token_count if ra else None,
                tokens_b=rb.token_count if rb else None,
                duration_a=ra.duration_seconds if ra else None,
                duration_b=rb.duration_seconds if rb else None,
            )
        )

    return BundleComparison(
        bundle_a_hash=bundle_a.bundle_hash(),
        bundle_b_hash=bundle_b.bundle_hash(),
        score_a=bundle_a.overall_score,
        score_b=bundle_b.overall_score,
        pass_rate_a=bundle_a.pass_rate,
        pass_rate_b=bundle_b.pass_rate,
        total_cost_a=bundle_a.total_cost_usd,
        total_cost_b=bundle_b.total_cost_usd,
        total_tokens_a=bundle_a.total_tokens,
        total_tokens_b=bundle_b.total_tokens,
        total_duration_a=bundle_a.total_duration_seconds,
        total_duration_b=bundle_b.total_duration_seconds,
        task_comparisons=task_comparisons,
    )
