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


def _baseline_signature_problem(baseline_bundle: SubmissionBundle) -> str | None:
    """Why *baseline_bundle* cannot count as a signed baseline, or ``None``.

    "Signed" is a precondition of #5458 ("the last signed bundle on the
    default branch"), so an unsigned bundle is refused outright. What can be
    checked beyond presence depends on the signer: a stub signature is
    recomputed and compared, so a bundle re-hashed after stub-signing is
    caught. An install-identity signature cannot be checked here -- nothing
    in the bench layer verifies one yet (#5856) -- and the scorecard says so
    rather than implying it was.
    """
    from bernstein.eval.bench.signer import StubSigner

    if not baseline_bundle.signature or not baseline_bundle.signer_fingerprint:
        return "Baseline bundle is unsigned; only a signed baseline is compared against."
    if baseline_bundle.signer_fingerprint == StubSigner.fingerprint() and not StubSigner.verify(baseline_bundle):
        return (
            "Baseline bundle's stub signature does not verify against its hash; the bundle was altered after signing."
        )
    return None


def _neutral(bundle: SubmissionBundle, summary: str, baseline_bundle: SubmissionBundle | None = None) -> BenchScorecard:
    return BenchScorecard(
        suite_version=bundle.suite_version,
        suite_hash=bundle.suite_hash,
        bundle_hash=bundle.bundle_hash(),
        pass_rate=bundle.pass_rate,
        overall_score=bundle.overall_score,
        baseline_pass_rate=None,
        baseline_score=None,
        pass_rate_delta=None,
        score_delta=None,
        conclusion="neutral",
        summary=summary,
        baseline_bundle_hash=baseline_bundle.bundle_hash() if baseline_bundle is not None else None,
    )


def evaluate_ci_scorecard(
    bundle: SubmissionBundle,
    suite: BenchSuite,
    baseline_bundle: SubmissionBundle | None = None,
    verifier: BenchVerifier | None = None,
    regression_threshold: float = 0.0,
) -> BenchScorecard:
    """Evaluate *bundle* against *baseline_bundle* and compute a conclusion.

    ``success`` and ``failure`` are only ever reached over a baseline that is
    signed, from *suite*, and re-verified by *verifier* (receipt hashes and
    replayed verdicts). Everything short of that is ``neutral`` with the
    reason in ``summary`` -- never a green (#5458). A missing *verifier* is
    one of those shortfalls, not a way to skip the check.
    """
    pass_rate = bundle.pass_rate
    score = bundle.overall_score

    if baseline_bundle is None:
        return _neutral(bundle, "No baseline bundle provided for comparison. Result is neutral.")

    if baseline_bundle.suite_hash != suite.suite_hash:
        return _neutral(
            bundle,
            f"Baseline bundle is from a different suite ({baseline_bundle.suite_version}, "
            f"{baseline_bundle.suite_hash[:12]}); a delta against it would compare different tasks.",
            baseline_bundle,
        )

    problem = _baseline_signature_problem(baseline_bundle)
    if problem is not None:
        return _neutral(bundle, problem, baseline_bundle)

    if verifier is None:
        return _neutral(
            bundle, "No verifier supplied, so the baseline was not re-verified. Result is neutral.", baseline_bundle
        )

    try:
        ver_res = verifier.verify(baseline_bundle)
    except Exception as exc:
        return _neutral(bundle, f"Baseline bundle verification raised {type(exc).__name__}: {exc}", baseline_bundle)
    if not ver_res.passed:
        return _neutral(
            bundle,
            f"Baseline bundle is unverifiable or tampered ({ver_res.status.value}). Result is neutral.",
            baseline_bundle,
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

    from bernstein.eval.bench.signer import StubSigner

    if baseline_bundle.signer_fingerprint != StubSigner.fingerprint():
        # A non-stub fingerprint is present but nothing in the bench layer can
        # verify it yet (#5856). A delta measured against an unverifiable
        # baseline must not read as success.
        return _neutral(
            bundle,
            f"Baseline signature by {baseline_bundle.signer_fingerprint} is present but cannot "
            "be verified by bench (#5856); result is neutral.",
            baseline_bundle,
        )

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
