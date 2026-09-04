"""bernstein-bench: runnable, reproducibility-gated evaluation harness."""

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.ci import (
    BenchScorecard,
    evaluate_ci_scorecard,
    post_bench_check_run,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.leaderboard import Leaderboard, LeaderboardEntry
from bernstein.eval.bench.reliability import (
    InstallIdentityReliabilitySigner,
    ReliabilityCheckResult,
    ReliabilityReceipt,
    ReliabilityRunner,
    ReliabilityVerificationResult,
    ReliabilityVerificationStatus,
    ReliabilityVerifier,
    StubReliabilitySigner,
    TaskReliabilityResult,
    TaskReliabilityVerification,
    coordination_hash,
    coordination_projection,
    first_divergent_coordination_field,
    reliability_check,
    validate_run_receipt,
)
from bernstein.eval.bench.runner import (
    BenchRunner,
    MockReplayAdapter,
    ReplayAdapter,
    StochasticMockReplayAdapter,
)
from bernstein.eval.bench.sarif import bundle_to_sarif
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import (
    BenchVerifier,
    BundleVerificationResult,
    TaskVerificationResult,
    VerificationStatus,
)

__all__ = [
    "BenchRunner",
    "BenchScorecard",
    "BenchSuite",
    "BenchTask",
    "BenchVerifier",
    "BundleVerificationResult",
    "InstallIdentityReliabilitySigner",
    "Leaderboard",
    "LeaderboardEntry",
    "MockReplayAdapter",
    "ReliabilityCheckResult",
    "ReliabilityReceipt",
    "ReliabilityRunner",
    "ReliabilityVerificationResult",
    "ReliabilityVerificationStatus",
    "ReliabilityVerifier",
    "ReplayAdapter",
    "StochasticMockReplayAdapter",
    "StubReliabilitySigner",
    "SubmissionBundle",
    "TaskReliabilityResult",
    "TaskReliabilityVerification",
    "TaskResult",
    "TaskVerificationResult",
    "VerificationStatus",
    "build_golden_suite_v1",
    "bundle_to_sarif",
    "coordination_hash",
    "coordination_projection",
    "evaluate_ci_scorecard",
    "first_divergent_coordination_field",
    "post_bench_check_run",
    "reliability_check",
    "validate_run_receipt",
]
