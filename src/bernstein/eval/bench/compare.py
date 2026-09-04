"""
bernstein-bench: submission bundle comparison.

Compares two benchmark submission bundles across accuracy, score, tokens,
wall-clock duration, and cost in USD.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle


@dataclass
class TaskComparison:
    """Per-task metrics comparison between two bundles."""

    task_id: str
    passed_a: bool
    passed_b: bool
    score_a: float
    score_b: float
    cost_a: float
    cost_b: float
    tokens_a: int
    tokens_b: int
    duration_a: float
    duration_b: float


@dataclass
class CompareResult:
    """Aggregated comparison report between two bundles."""

    bundle_a_hash: str
    bundle_b_hash: str
    pass_rate_a: float
    pass_rate_b: float
    pass_rate_delta: float
    score_a: float
    score_b: float
    score_delta: float
    cost_a_usd: float
    cost_b_usd: float
    cost_delta_usd: float
    cost_delta_percent: float
    tokens_a: int
    tokens_b: int
    tokens_delta: int
    duration_a_seconds: float
    duration_b_seconds: float
    duration_delta_seconds: float
    task_comparisons: list[TaskComparison] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        """Format comparison as a Markdown summary table."""
        lines = [
            "# Benchmark Bundle Comparison",
            "",
            f"- **Bundle A**: `{self.bundle_a_hash}`",
            f"- **Bundle B**: `{self.bundle_b_hash}`",
            "",
            "## Summary Metrics",
            "",
            "| Metric | Bundle A | Bundle B | Delta |",
            "| :--- | :--- | :--- | :--- |",
            (
                f"| **Pass Rate** | {self.pass_rate_a * 100:.1f}% | "
                f"{self.pass_rate_b * 100:.1f}% | {self.pass_rate_delta * 100:+.1f}% |"
            ),
            (f"| **Score** | {self.score_a * 100:.1f}% | {self.score_b * 100:.1f}% | {self.score_delta * 100:+.1f}% |"),
            (
                f"| **Cost (USD)** | ${self.cost_a_usd:.4f} | ${self.cost_b_usd:.4f} | "
                f"${self.cost_delta_usd:+.4f} ({self.cost_delta_percent:+.1f}%) |"
            ),
            f"| **Tokens** | {self.tokens_a:,} | {self.tokens_b:,} | {self.tokens_delta:+,} |",
            (
                f"| **Duration** | {self.duration_a_seconds:.2f}s | "
                f"{self.duration_b_seconds:.2f}s | {self.duration_delta_seconds:+.2f}s |"
            ),
            "",
        ]

        if self.task_comparisons:
            lines.extend(
                [
                    "## Per-Task Breakdown",
                    "",
                    "| Task ID | Pass A | Pass B | Score A | Score B | Cost A ($) | Cost B ($) | Tokens A | Tokens B |",
                    "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
                ]
            )
            for tc in self.task_comparisons:
                p_a = "✓" if tc.passed_a else "✗"
                p_b = "✓" if tc.passed_b else "✗"
                lines.append(
                    f"| `{tc.task_id}` | {p_a} | {p_b} | {tc.score_a:.2f} | {tc.score_b:.2f} | "
                    f"${tc.cost_a:.4f} | ${tc.cost_b:.4f} | {tc.tokens_a:,} | {tc.tokens_b:,} |"
                )
            lines.append("")

        return "\n".join(lines)


def compare_bundles(bundle_a: SubmissionBundle, bundle_b: SubmissionBundle) -> CompareResult:
    """Compare two submission bundles and return their delta metrics."""
    pass_rate_a = bundle_a.pass_rate
    pass_rate_b = bundle_b.pass_rate
    pass_rate_delta = pass_rate_b - pass_rate_a

    score_a = bundle_a.overall_score
    score_b = bundle_b.overall_score
    score_delta = score_b - score_a

    cost_a = bundle_a.total_cost_usd
    cost_b = bundle_b.total_cost_usd
    cost_delta = cost_b - cost_a
    cost_delta_pct = ((cost_b - cost_a) / cost_a * 100.0) if cost_a > 0 else 0.0

    tokens_a = bundle_a.total_tokens
    tokens_b = bundle_b.total_tokens
    tokens_delta = tokens_b - tokens_a

    dur_a = bundle_a.total_duration_seconds
    dur_b = bundle_b.total_duration_seconds
    dur_delta = dur_b - dur_a

    b_map = {r.task_id: r for r in bundle_b.task_results}
    all_task_ids = list(
        dict.fromkeys([r.task_id for r in bundle_a.task_results] + [r.task_id for r in bundle_b.task_results])
    )

    task_cmps: list[TaskComparison] = []
    a_map = {r.task_id: r for r in bundle_a.task_results}

    for tid in all_task_ids:
        ra = a_map.get(tid)
        rb = b_map.get(tid)
        task_cmps.append(
            TaskComparison(
                task_id=tid,
                passed_a=ra.passed if ra else False,
                passed_b=rb.passed if rb else False,
                score_a=ra.score if ra else 0.0,
                score_b=rb.score if rb else 0.0,
                cost_a=ra.cost_usd if ra else 0.0,
                cost_b=rb.cost_usd if rb else 0.0,
                tokens_a=ra.tokens if ra else 0,
                tokens_b=rb.tokens if rb else 0,
                duration_a=ra.duration_seconds if ra else 0.0,
                duration_b=rb.duration_seconds if rb else 0.0,
            )
        )

    return CompareResult(
        bundle_a_hash=bundle_a.bundle_hash(),
        bundle_b_hash=bundle_b.bundle_hash(),
        pass_rate_a=pass_rate_a,
        pass_rate_b=pass_rate_b,
        pass_rate_delta=pass_rate_delta,
        score_a=score_a,
        score_b=score_b,
        score_delta=score_delta,
        cost_a_usd=cost_a,
        cost_b_usd=cost_b,
        cost_delta_usd=cost_delta,
        cost_delta_percent=cost_delta_pct,
        tokens_a=tokens_a,
        tokens_b=tokens_b,
        tokens_delta=tokens_delta,
        duration_a_seconds=dur_a,
        duration_b_seconds=dur_b,
        duration_delta_seconds=dur_delta,
        task_comparisons=task_cmps,
    )
