"""bernstein-bench: runnable, reproducibility-gated evaluation harness."""

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.contamination import (
    ContaminationVerdict,
    admit_task,
    check_solution_contamination,
    extract_ngrams,
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
from bernstein.eval.bench.rotation import (
    RotationStatus,
    check_suite_saturation,
)
from bernstein.eval.bench.runner import (
    BenchRunner,
    HoldoutBenchRunner,
    HoldoutIsolationError,
    MockReplayAdapter,
    ReplayAdapter,
    StochasticMockReplayAdapter,
)
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.tool_surface_suite import (
    ToolSurfaceReplayAdapter,
    build_tool_surface_suite,
)
from bernstein.eval.bench.verifier import (
    BenchVerifier,
    BundleVerificationResult,
    TaskVerificationResult,
    VerificationStatus,
)

__all__ = [
    "BenchRunner",
    "BenchSuite",
    "BenchTask",
    "BenchVerifier",
    "BundleVerificationResult",
    "ContaminationVerdict",
    "HoldoutBenchRunner",
    "HoldoutIsolationError",
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
    "RotationStatus",
    "StochasticMockReplayAdapter",
    "StubReliabilitySigner",
    "SubmissionBundle",
    "TaskReliabilityResult",
    "TaskReliabilityVerification",
    "TaskResult",
    "TaskVerificationResult",
    "ToolSurfaceReplayAdapter",
    "VerificationStatus",
    "admit_task",
    "build_golden_suite_v1",
    "build_tool_surface_suite",
    "check_solution_contamination",
    "check_suite_saturation",
    "coordination_hash",
    "coordination_projection",
    "extract_ngrams",
    "first_divergent_coordination_field",
    "reliability_check",
    "validate_run_receipt",
]
