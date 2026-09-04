"""
bernstein-bench: CI scorecard and baseline delta evaluation.

Evaluates benchmark run results against signed baselines, formats Markdown
scorecard tables, and publishes GitHub Check Runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.suite import BenchSuite
    from bernstein.eval.bench.verifier import BenchVerifier
    from bernstein.github_app.check_runs import CheckRunClient, CheckRunResult


@dataclass
class BenchScorecard:
    """Benchmark scorecard comparing current run against baseline."""

    suite_version: str
    suite_hash: str
    bundle_hash: str
    pass_rate: float
    overall_score: float
    baseline_pass_rate: float | None
    baseline_score: float | None
    pass_rate_delta: float | None
    score_delta: float | None
    conclusion: str  # "success", "failure", "neutral"
    summary: str
    baseline_bundle_hash: str | None = None

    def to_markdown(self) -> str:
        """Format scorecard as a GitHub Markdown table."""
        delta_str = "N/A"
        if self.pass_rate_delta is not None:
            delta_str = f"{self.pass_rate_delta * 100:+.1f}%"

        baseline_str = "N/A"
        if self.baseline_pass_rate is not None:
            baseline_str = f"{self.baseline_pass_rate * 100:.1f}%"

        if self.conclusion == "success":
            status_str = "✓ PASS"
        elif self.conclusion == "failure":
            status_str = "✗ FAIL"
        else:
            status_str = "⚪ NEUTRAL"

        lines = [
            "### Benchmark CI Scorecard",
            "",
            "| Suite | Pass Rate | Score | Baseline Pass Rate | Delta | Bundle Hash | Status |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
            (
                f"| `{self.suite_version}` | {self.pass_rate * 100:.1f}% | "
                f"{self.overall_score:.2f} | {baseline_str} | {delta_str} | "
                f"`{self.bundle_hash[:12]}` | {status_str} |"
            ),
            "",
            f"**Summary**: {self.summary}",
        ]
        return "\n".join(lines)


def evaluate_ci_scorecard(
    bundle: SubmissionBundle,
    suite: BenchSuite,
    baseline_bundle: SubmissionBundle | None = None,
    verifier: BenchVerifier | None = None,
    regression_threshold: float = 0.0,
) -> BenchScorecard:
    """Evaluate current bundle against baseline and compute conclusion."""
    pass_rate = bundle.pass_rate
    score = bundle.overall_score

    if baseline_bundle is None:
        return BenchScorecard(
            suite_version=bundle.suite_version,
            suite_hash=bundle.suite_hash,
            bundle_hash=bundle.bundle_hash(),
            pass_rate=pass_rate,
            overall_score=score,
            baseline_pass_rate=None,
            baseline_score=None,
            pass_rate_delta=None,
            score_delta=None,
            conclusion="neutral",
            summary="No baseline bundle provided for comparison. Result is neutral.",
        )

    # Verify baseline bundle integrity if verifier provided
    if verifier is not None:
        try:
            ver_res = verifier.verify(baseline_bundle)
            if not ver_res.passed:
                return BenchScorecard(
                    suite_version=bundle.suite_version,
                    suite_hash=bundle.suite_hash,
                    bundle_hash=bundle.bundle_hash(),
                    pass_rate=pass_rate,
                    overall_score=score,
                    baseline_pass_rate=None,
                    baseline_score=None,
                    pass_rate_delta=None,
                    score_delta=None,
                    conclusion="neutral",
                    summary="Baseline bundle is unverifiable or tampered. Result is neutral.",
                    baseline_bundle_hash=baseline_bundle.bundle_hash(),
                )
        except Exception:
            return BenchScorecard(
                suite_version=bundle.suite_version,
                suite_hash=bundle.suite_hash,
                bundle_hash=bundle.bundle_hash(),
                pass_rate=pass_rate,
                overall_score=score,
                baseline_pass_rate=None,
                baseline_score=None,
                pass_rate_delta=None,
                score_delta=None,
                conclusion="neutral",
                summary="Baseline bundle verification failed. Result is neutral.",
                baseline_bundle_hash=baseline_bundle.bundle_hash(),
            )

    b_pass_rate = baseline_bundle.pass_rate
    b_score = baseline_bundle.overall_score
    pass_delta = pass_rate - b_pass_rate
    score_delta = score - b_score

    # Check for regression beyond threshold
    if pass_delta < -regression_threshold:
        conclusion = "failure"
        summary = (
            f"Regression detected: pass rate dropped by {abs(pass_delta) * 100:.1f}% "
            f"(threshold allowed: {regression_threshold * 100:.1f}%)."
        )
    else:
        conclusion = "success"
        summary = f"Benchmark passed successfully with delta {pass_delta * 100:+.1f}% vs baseline."

    return BenchScorecard(
        suite_version=bundle.suite_version,
        suite_hash=bundle.suite_hash,
        bundle_hash=bundle.bundle_hash(),
        pass_rate=pass_rate,
        overall_score=score,
        baseline_pass_rate=b_pass_rate,
        baseline_score=b_score,
        pass_rate_delta=pass_delta,
        score_delta=score_delta,
        conclusion=conclusion,
        summary=summary,
        baseline_bundle_hash=baseline_bundle.bundle_hash(),
    )


def post_bench_check_run(
    scorecard: BenchScorecard,
    client: CheckRunClient,
    head_sha: str,
) -> CheckRunResult | None:
    """Publish a benchmark scorecard check run to GitHub."""
    return client.create_bench_check_run(
        head_sha=head_sha,
        summary=scorecard.summary,
        scorecard_table=scorecard.to_markdown(),
        conclusion=scorecard.conclusion,
    )
